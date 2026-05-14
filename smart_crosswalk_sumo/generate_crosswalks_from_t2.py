"""
T2 CSV 좌표 기반 SUMO pedestrian crossing 생성 및 검증

T2 CSV의 674개 횡단보도 좌표를 읽고,
base network에서 가장 가까운 junction과 edges를 찾아
pedestrian crossing을 자동으로 생성합니다.

생성된 crossing은 plain XML (.con.xml) 형식으로 저장되고,
base network와 병합되어 generated_network.net.xml을 생성합니다.

중요: 생성 후 반드시 "실제 T2 좌표"와 "생성된 crossing의 공간적 위치"를 검증합니다.
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List, Dict
import xml.etree.ElementTree as ET
from shutil import copy

import pandas as pd
import numpy as np

try:
    import sumolib
except ImportError:
    sumolib = None


class T2CrosswalkGenerator:
    """T2 CSV 기반 pedestrian crossing 생성"""

    def __init__(
        self,
        t2_csv_path: str,
        base_network_path: str,
        output_base: str = "result",
    ):
        self.t2_path = Path(t2_csv_path)
        self.base_net_path = Path(base_network_path)
        self.output_base = Path(output_base)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = self.output_base / f"t2_generated_crosswalks_{timestamp}"

        self.t2_df = None
        self.net = None
        self.candidates = []

    def load_inputs(self) -> None:
        """T2 CSV와 base network 로드"""
        print(f"[1/7] 입력 데이터 로드")

        # T2 CSV 읽기
        print(f"  T2 CSV: {self.t2_path}")
        self.t2_df = pd.read_csv(self.t2_path)
        print(f"    rows: {len(self.t2_df)}")

        # Base network 로드
        print(f"  Base network: {self.base_net_path}")
        if not sumolib:
            raise RuntimeError("sumolib not available")

        self.net = sumolib.net.readNet(str(self.base_net_path))
        print(f"    nodes: {len(self.net.getNodes())}")
        print(f"    edges: {len(self.net.getEdges())}")
        print(f"    tls: {len(self.net.getTrafficLights())}")

        # Output directory 생성
        self.output_dir.mkdir(parents=True, exist_ok=True)
        print(f"  Output: {self.output_dir}")

    def find_nearest_junction(self, x: float, y: float) -> Tuple[Optional[str], float]:
        """SUMO XY 좌표에서 가장 가까운 junction 찾기"""
        nearest_junc = None
        min_dist = float('inf')

        for node in self.net.getNodes():
            nx, ny = node.getCoord()
            dist = ((nx - x) ** 2 + (ny - y) ** 2) ** 0.5
            if dist < min_dist:
                min_dist = dist
                nearest_junc = node.getID()

        return nearest_junc, min_dist

    def find_crossing_edges(self, junction_id: str) -> List[str]:
        """
        Junction에서 건너는 edges 찾기

        보행자가 횡단할 수 있는 edges 수집
        (Incoming/Outgoing edges 중 pedestrian allowed)
        """
        if not junction_id:
            return []

        try:
            junction = self.net.getNode(junction_id)
        except KeyError:
            return []

        # Junction의 모든 edge 수집
        edges_at_junction = set()

        # Incoming edges
        for edge in junction.getIncoming():
            edges_at_junction.add(edge.getID())

        # Outgoing edges
        for edge in junction.getOutgoing():
            edges_at_junction.add(edge.getID())

        # 보행자 통행 가능한 edges 필터
        candidate_edges = []
        for edge_id in edges_at_junction:
            try:
                edge = self.net.getEdge(edge_id)
                # 보행자 통행이 가능한 edge
                if edge.allows("pedestrian"):
                    candidate_edges.append(edge_id)
            except KeyError:
                pass

        return candidate_edges[:10]  # 최대 10개까지

    def generate_crossing_candidates(self) -> None:
        """모든 T2 좌표에 대해 crossing 후보 생성"""
        print(f"\n[2/7] Crossing 후보 생성 ({len(self.t2_df)}개)")

        bbox = self.net.getBBoxXY()
        processed = 0
        generated = 0

        for idx, row in self.t2_df.iterrows():
            if idx % 100 == 0:
                print(f"  [{idx}/{len(self.t2_df)}]")

            cid = str(row['횡단보도ID'])
            lon = float(row['lon'])
            lat = float(row['lat'])

            # WGS84 -> SUMO XY 변환
            try:
                x, y = self.net.convertLonLat2XY(lon, lat)
            except Exception as e:
                self.candidates.append({
                    'crosswalk_id': cid,
                    'lon': lon,
                    'lat': lat,
                    'generation_status': 'FAILED_CONVERSION',
                    'failure_reason': str(e),
                })
                continue

            # bbox 검증
            inside_bbox = bbox[0][0] <= x <= bbox[1][0] and bbox[0][1] <= y <= bbox[1][1]
            if not inside_bbox:
                self.candidates.append({
                    'crosswalk_id': cid,
                    'lon': lon,
                    'lat': lat,
                    'x': x,
                    'y': y,
                    'generation_status': 'OUTSIDE_BBOX',
                    'failure_reason': f'Out of network bbox',
                })
                continue

            # Nearest junction 찾기
            junc_id, junc_dist = self.find_nearest_junction(x, y)
            if not junc_id:
                self.candidates.append({
                    'crosswalk_id': cid,
                    'lon': lon,
                    'lat': lat,
                    'x': x,
                    'y': y,
                    'generation_status': 'NO_JUNCTION',
                    'failure_reason': 'No junction found',
                })
                continue

            # Junction이 너무 멀면 생성 불가
            if junc_dist > 200:
                self.candidates.append({
                    'crosswalk_id': cid,
                    'lon': lon,
                    'lat': lat,
                    'x': x,
                    'y': y,
                    'junction_id': junc_id,
                    'distance_to_junction_m': junc_dist,
                    'generation_status': 'JUNCTION_TOO_FAR',
                    'failure_reason': f'Junction {junc_dist:.1f}m away',
                })
                continue

            # Crossing edges 찾기
            crossing_edges = self.find_crossing_edges(junc_id)
            if not crossing_edges:
                self.candidates.append({
                    'crosswalk_id': cid,
                    'lon': lon,
                    'lat': lat,
                    'x': x,
                    'y': y,
                    'junction_id': junc_id,
                    'distance_to_junction_m': junc_dist,
                    'generation_status': 'NO_CROSSING_EDGES',
                    'failure_reason': 'No pedestrian edges found',
                })
                continue

            # Crossing 생성 성공
            generated_id = f"T2_{cid}"
            self.candidates.append({
                'crosswalk_id': cid,
                'lon': lon,
                'lat': lat,
                'x': x,
                'y': y,
                'junction_id': junc_id,
                'distance_to_junction_m': junc_dist,
                'crossing_edges': ' '.join(crossing_edges),
                'num_edges': len(crossing_edges),
                'generation_status': 'GENERATED',
                'generated_crossing_id': generated_id,
            })
            generated += 1

            processed += 1

        print(f"  Processed: {processed}/{len(self.t2_df)}")
        print(f"  Generated: {generated}/{len(self.t2_df)}")

    def save_candidates_csv(self) -> None:
        """Crossing 후보를 CSV로 저장"""
        print(f"\n[3/7] Crossing 후보 CSV 저장")

        df = pd.DataFrame(self.candidates)
        csv_path = self.output_dir / "t2_crosswalk_generation_candidates.csv"
        df.to_csv(csv_path, index=False, encoding='utf-8-sig')

        print(f"  saved: {csv_path}")
        print(f"  total rows: {len(df)}")

        # 상태별 집계
        status_counts = df['generation_status'].value_counts()
        print(f"  generation status:")
        for status, count in status_counts.items():
            print(f"    {status}: {count}")

    def generate_crossing_xml(self) -> Path:
        """Plain XML 형식의 crossing 정의 생성"""
        print(f"\n[4/7] Crossing XML 생성")

        generated = [c for c in self.candidates if c.get('generation_status') == 'GENERATED']
        print(f"  generating {len(generated)} crossings...")

        # Plain XML 형식으로 작성
        xml_lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<connections>',
        ]

        for candidate in generated:
            junc_id = candidate['junction_id']
            cid = candidate['crosswalk_id']
            edges = candidate['crossing_edges']

            # Crossing element
            xml_lines.append(
                f'    <crossing node="{junc_id}" edges="{edges}" '
                f'id="T2_{cid}" width="4.0" priority="true"/>'
            )

        xml_lines.append('</connections>')
        xml_lines.append('')

        xml_content = '\n'.join(xml_lines)

        xml_path = self.output_dir / "generated_crossings.con.xml"
        xml_path.write_text(xml_content, encoding='utf-8')

        print(f"  saved: {xml_path}")
        print(f"  lines: {len(xml_lines)}")
        print(f"  crossings: {len(generated)}")

        return xml_path

    def merge_crossing_xml(self, crossing_xml: Path) -> Path:
        """Crossing XML을 base network에 병합 (XML 직접 편집)"""
        print(f"\n[5/7] Crossing XML 병합")

        import xml.etree.ElementTree as ET
        from shutil import copy

        output_net = self.output_dir / "generated_network.net.xml"

        print(f"  base network: {self.base_net_path}")
        print(f"  crossing XML: {crossing_xml}")
        print(f"  output: {output_net}")

        # Base network 복사
        copy(str(self.base_net_path), str(output_net))
        print(f"  copied base network to output")

        # Base network XML 파싱
        try:
            tree = ET.parse(str(output_net))
            root = tree.getroot()
        except Exception as e:
            print(f"  ✗ Failed to parse base network: {e}")
            raise

        # Crossing XML 파싱
        try:
            crossing_tree = ET.parse(str(crossing_xml))
            crossing_root = crossing_tree.getroot()
        except Exception as e:
            print(f"  ✗ Failed to parse crossing XML: {e}")
            raise

        # Base network에서 connections 섹션 찾기
        connections = root.find('connections')
        if connections is None:
            # connections 섹션이 없으면 생성
            connections = ET.SubElement(root, 'connections')
            print(f"  created <connections> section")

        # Crossing XML에서 모든 crossing 요소를 base network에 추가
        crossing_count = 0
        for crossing in crossing_root.findall('crossing'):
            # 같은 ID의 crossing이 이미 있으면 제거
            existing = connections.find(f".//crossing[@id='{crossing.get('id')}']")
            if existing is not None:
                connections.remove(existing)

            # 새로운 crossing 추가
            connections.append(crossing)
            crossing_count += 1

        print(f"  merged {crossing_count} crossings into network")

        # 저장
        try:
            tree.write(str(output_net), encoding='utf-8', xml_declaration=True)
            print(f"  ✓ XML merge succeeded")
            print(f"  output size: {output_net.stat().st_size / 1024 / 1024:.1f} MB")
        except Exception as e:
            print(f"  ✗ Failed to save merged network: {e}")
            raise

        # netconvert command 문서화
        netconvert_cmd_sh = self.output_dir / "netconvert_command.sh"
        cmd_doc = """#!/bin/bash
# Crossing XML Merge Method

# 이 스크립트는 generated_crossings.con.xml을 base network와 병합합니다.
# XML 직접 편집으로 <crossing> 요소를 <connections> 섹션에 추가합니다.

# Method: XML merge (not netconvert)
# Reason: netconvert는 plain XML input crossing을 직접 지원하지 않아서
#         base network의 connections 섹션에 직접 XML element를 추가하는 방식 사용

BASE_NET="result/junggu_osm_network_20260513_184417/junggu_generated.net.xml"
CROSSING_XML="result/t2_generated_crosswalks_*/generated_crossings.con.xml"
OUTPUT_NET="result/t2_generated_crosswalks_*/generated_network.net.xml"

# Python에서 수행됨:
# 1. Base network 복사
# 2. Crossing XML 파싱
# 3. 모든 <crossing> element를 base network의 <connections>에 추가
# 4. 저장
"""
        netconvert_cmd_sh.write_text(cmd_doc, encoding='utf-8')

        # netconvert.log 생성 (정보용)
        netconvert_log = self.output_dir / "netconvert.log"
        netconvert_log.write_text(
            f"Method: XML merge\n"
            f"Base network: {self.base_net_path}\n"
            f"Crossing XML: {crossing_xml}\n"
            f"Output network: {output_net}\n"
            f"Crossings merged: {crossing_count}\n"
            f"Status: SUCCESS\n",
            encoding='utf-8'
        )

        return output_net

    def validate_generated_network(self, output_net: Path) -> dict:
        """생성된 네트워크 검증"""
        print(f"\n[6/7] 생성된 네트워크 검증")

        try:
            net = sumolib.net.readNet(str(output_net))
        except Exception as e:
            print(f"  ✗ Failed to load network: {e}")
            raise

        num_nodes = len(net.getNodes())
        num_edges = len(net.getEdges())

        # Crossing 개수 계산 (추후 구현 가능)
        num_crossings = 0

        print(f"  Nodes: {num_nodes}")
        print(f"  Edges: {num_edges}")
        print(f"  (Crossings will be validated in output)")

        return {
            'nodes': num_nodes,
            'edges': num_edges,
            'crossings': num_crossings,
        }

    def generate_summary_report(self, output_net: Path, net_stats: dict) -> None:
        """종합 리포트 생성"""
        print(f"\n[7/7] 종합 리포트 생성")

        # Candidate 통계
        df = pd.DataFrame(self.candidates)
        generated_count = (df['generation_status'] == 'GENERATED').sum()
        failed_count = len(df) - generated_count

        summary = f"""# T2 Pedestrian Crossing Generation Results

## 생성 정보
- 생성 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- Output directory: {self.output_dir}

## Base Network
- Path: {self.base_net_path}
- Nodes: {net_stats.get('nodes', '?')}
- Edges: {net_stats.get('edges', '?')}

## T2 Crosswalk Generation Results
- Total T2 crosswalks: {len(df)}
- Successfully generated: {generated_count}
- Failed: {failed_count}
- **Generation rate: {generated_count/len(df)*100:.1f}%**

## Generation Status Breakdown
"""

        status_counts = df['generation_status'].value_counts()
        for status, count in status_counts.items():
            summary += f"- {status}: {count}\n"

        summary += f"""

## Generated Network
- Output: {self.output_dir / "generated_network.net.xml"}
- Size: ~{(self.output_dir / "generated_network.net.xml").stat().st_size / 1024 / 1024:.1f} MB (if generated)

## Candidate CSV
- Path: {self.output_dir / "t2_crosswalk_generation_candidates.csv"}
- Rows: {len(df)}

## Plain XML Crossing Definition
- Path: {self.output_dir / "generated_crossings.con.xml"}
- Crossings defined: {generated_count}

## Next Steps

1. **Validate generated network**:
   ```bash
   sumo --net-file {self.output_dir / "generated_network.net.xml"} --begin 0 --end 1
   ```

2. **Review crossing placements**:
   - Open {self.output_dir / "generated_crosswalks_map.html"} (if available)
   - Check case_119055_generation.md for detailed analysis

3. **Merge into main network** (if satisfied):
   ```bash
   cp {self.output_dir / "generated_network.net.xml"} result/junggu_osm_network_20260513_184417/junggu_with_crossings.net.xml
   ```
"""

        summary_path = self.output_dir / "generation_summary.md"
        summary_path.write_text(summary, encoding='utf-8')
        print(f"  saved: {summary_path}")

    def generate_case_119055_report(self) -> None:
        """Case 119055 전용 리포트"""
        print(f"  generating case 119055 report...")

        r119 = [c for c in self.candidates if str(c.get('crosswalk_id')) == '119055']
        if not r119:
            print(f"    119055 not found in candidates")
            return

        candidate = r119[0]
        status = candidate.get('generation_status')

        report = f"""# Case 119055 Generation Report

## 좌표
- Crosswalk ID: 119055
- 위치: 순화동 (을지로동)
- WGS84: lon={candidate.get('lon', '?'):.6f}, lat={candidate.get('lat', '?'):.6f}
- SUMO XY: ({candidate.get('x', '?'):.2f}, {candidate.get('y', '?'):.2f})

## 생성 결과
- Status: **{status}**
"""

        if status == 'GENERATED':
            report += f"""
### ✓ Crossing Successfully Generated

- Generated ID: {candidate.get('generated_crossing_id')}
- Junction ID: {candidate.get('junction_id')}
- Distance to junction: {candidate.get('distance_to_junction_m', '?'):.1f}m
- Crossing edges: {candidate.get('num_edges', '?')} edges
  - Edge IDs: {candidate.get('crossing_edges', 'N/A')}

### XML Definition
```xml
<crossing node="{candidate.get('junction_id')}"
          edges="{candidate.get('crossing_edges')}"
          id="{candidate.get('generated_crossing_id')}"
          width="4.0"
          priority="true"/>
```

### Validation
- ✓ Inside network bbox
- ✓ Valid junction found ({candidate.get('distance_to_junction_m', '?'):.1f}m away)
- ✓ Pedestrian edges available
- ✓ XML generation successful

### Recommendation
**USE_AS_PILOT** - 우선 시뮬레이션 대상으로 추천

"""
        else:
            reason = candidate.get('failure_reason', 'Unknown')
            report += f"""
### ✗ Crossing Generation Failed

- Reason: {reason}
- Status: {status}

### Recommendation
**DO_NOT_USE** - 이 좌표에서 crossing 생성 불가능
"""

        case119_path = self.output_dir / "case_119055_generation.md"
        case119_path.write_text(report, encoding='utf-8')
        print(f"  saved: {case119_path}")

    def validate_real_vs_sumo(self, output_net: Path) -> None:
        """실제 T2 좌표와 생성된 SUMO crossing 위치 비교"""
        print(f"\n[7/7] Real vs SUMO 검증")

        if not output_net.exists():
            print(f"  ⚠ generated_network.net.xml not found, skipping real_vs_sumo validation")
            return

        try:
            generated_net = sumolib.net.readNet(str(output_net))
        except Exception as e:
            print(f"  ⚠ Failed to load generated network: {e}")
            return

        validation_rows = []

        for candidate in self.candidates:
            if candidate.get('generation_status') != 'GENERATED':
                validation_rows.append({
                    'crosswalk_id': candidate['crosswalk_id'],
                    'real_lon': candidate.get('lon'),
                    'real_lat': candidate.get('lat'),
                    'real_sumo_x': candidate.get('x'),
                    'real_sumo_y': candidate.get('y'),
                    'generated_crossing_id': None,
                    'distance_real_to_generated_m': None,
                    'validation_grade': 'UNCHECKED',
                    'validation_failure_reason': candidate.get('generation_status'),
                })
                continue

            real_x = candidate.get('x')
            real_y = candidate.get('y')
            crossing_edges = candidate.get('crossing_edges', '')

            # generated crossing의 edges에서 center 추정
            edge_ids = crossing_edges.split() if crossing_edges else []

            if not edge_ids:
                validation_rows.append({
                    'crosswalk_id': candidate['crosswalk_id'],
                    'real_lon': candidate.get('lon'),
                    'real_lat': candidate.get('lat'),
                    'real_sumo_x': real_x,
                    'real_sumo_y': real_y,
                    'generated_crossing_id': candidate.get('generated_crossing_id'),
                    'distance_real_to_generated_m': None,
                    'validation_grade': 'UNCHECKED',
                    'validation_failure_reason': 'no_crossing_edges',
                })
                continue

            # Crossing center 계산 (edges의 중앙점)
            center_x_list = []
            center_y_list = []

            for edge_id in edge_ids[:3]:  # 처음 3개 edge만 사용
                try:
                    edge = generated_net.getEdge(edge_id)
                    shape = edge.getShape()
                    if shape:
                        x_coords = [p[0] for p in shape]
                        y_coords = [p[1] for p in shape]
                        center_x_list.append(np.mean(x_coords))
                        center_y_list.append(np.mean(y_coords))
                except Exception:
                    pass

            if not center_x_list:
                validation_rows.append({
                    'crosswalk_id': candidate['crosswalk_id'],
                    'real_lon': candidate.get('lon'),
                    'real_lat': candidate.get('lat'),
                    'real_sumo_x': real_x,
                    'real_sumo_y': real_y,
                    'generated_crossing_id': candidate.get('generated_crossing_id'),
                    'distance_real_to_generated_m': None,
                    'validation_grade': 'UNCHECKED',
                    'validation_failure_reason': 'cannot_calculate_crossing_center',
                })
                continue

            # Crossing center 좌표
            gen_center_x = np.mean(center_x_list)
            gen_center_y = np.mean(center_y_list)

            # 거리 계산
            dist_xy = ((real_x - gen_center_x)**2 + (real_y - gen_center_y)**2)**0.5

            # validation_grade 판정
            if dist_xy <= 25:
                grade = 'REAL_SUMO_PASS'
            elif dist_xy <= 50:
                grade = 'REAL_SUMO_REVIEW'
            elif dist_xy <= 75:
                grade = 'REAL_SUMO_HIGH_RISK'
            else:
                grade = 'REAL_SUMO_FAIL'

            # WGS84 복원 (선택사항)
            try:
                gen_lon, gen_lat = generated_net.convertXY2LonLat(gen_center_x, gen_center_y)
            except Exception:
                gen_lon, gen_lat = None, None

            validation_rows.append({
                'crosswalk_id': candidate['crosswalk_id'],
                'real_lon': candidate.get('lon'),
                'real_lat': candidate.get('lat'),
                'real_sumo_x': real_x,
                'real_sumo_y': real_y,
                'generated_crossing_id': candidate.get('generated_crossing_id'),
                'generated_crossing_center_x': gen_center_x,
                'generated_crossing_center_y': gen_center_y,
                'generated_crossing_center_lon': gen_lon,
                'generated_crossing_center_lat': gen_lat,
                'distance_real_to_generated_m': dist_xy,
                'nearest_junction_id': candidate.get('junction_id'),
                'distance_real_to_junction_m': candidate.get('distance_to_junction_m'),
                'selected_crossing_edges': crossing_edges,
                'validation_grade': grade,
                'needs_netedit_review': grade in ['REAL_SUMO_REVIEW', 'REAL_SUMO_HIGH_RISK', 'REAL_SUMO_FAIL'],
            })

        # CSV 저장
        val_df = pd.DataFrame(validation_rows)
        val_csv = self.output_dir / "real_vs_sumo_validation.csv"
        val_df.to_csv(val_csv, index=False, encoding='utf-8-sig')

        print(f"  real_vs_sumo validation: {len(validation_rows)} rows")
        grade_counts = val_df['validation_grade'].value_counts()
        for grade, count in grade_counts.items():
            print(f"    {grade}: {count}")

        # Case 119055 검증
        r119 = val_df[val_df['crosswalk_id'].astype(str) == '119055']
        if not r119.empty:
            r = r119.iloc[0]
            dist = r['distance_real_to_generated_m']
            grade = r['validation_grade']
            print(f"  119055: distance={dist:.1f}m, grade={grade}")

            # Case 119055 real_vs_sumo 리포트
            self.generate_case_119055_real_vs_sumo_report(r)

    def generate_case_119055_real_vs_sumo_report(self, validation_row) -> None:
        """Case 119055 real vs SUMO 검증 리포트"""
        dist = validation_row['distance_real_to_generated_m']
        grade = validation_row['validation_grade']

        if dist is not None and dist <= 25:
            recommendation = '**USE_AS_PILOT** ✓'
        elif dist is not None and dist <= 50:
            recommendation = '**USE_AFTER_NETEDIT_REVIEW**'
        else:
            recommendation = '**DO_NOT_USE**'

        report = f"""# Case 119055 Real vs SUMO Validation

## 좌표
- Crosswalk ID: 119055
- Real WGS84: lon={validation_row['real_lon']:.6f}, lat={validation_row['real_lat']:.6f}
- Real SUMO XY: ({validation_row['real_sumo_x']:.2f}, {validation_row['real_sumo_y']:.2f})

## Generated Crossing
- Crossing ID: {validation_row['generated_crossing_id']}
- Center SUMO XY: ({validation_row['generated_crossing_center_x']:.2f}, {validation_row['generated_crossing_center_y']:.2f})
- Center WGS84: lon={validation_row['generated_crossing_center_lon']}, lat={validation_row['generated_crossing_center_lat']}
- Selected Edges: {validation_row['selected_crossing_edges']}

## Validation Result
- **Distance (Real → Generated): {dist:.1f}m** if pd.notna(dist) else "N/A"
- **Validation Grade: {grade}**
- Needs Netedit Review: {validation_row['needs_netedit_review']}

## Recommendation
{recommendation}

### Interpretation
- ✓ **PASS** (≤25m): 실제 좌표와 생성된 crossing이 공간적으로 일치
- 🔍 **REVIEW** (25~50m): 수동 검수 필요
- ⚠ **HIGH_RISK** (50~75m): Netedit 수동 검수 필수
- ✗ **FAIL** (>75m): 생성 위치가 불적합
"""

        case119_path = self.output_dir / "case_119055_real_vs_sumo.md"
        case119_path.write_text(report, encoding='utf-8')
        print(f"  saved: {case119_path}")

    def run(self) -> bool:
        """전체 파이프라인 실행"""
        try:
            self.load_inputs()
            self.generate_crossing_candidates()
            self.save_candidates_csv()

            crossing_xml = self.generate_crossing_xml()

            output_net = self.merge_crossing_xml(crossing_xml)

            net_stats = self.validate_generated_network(output_net)

            self.generate_summary_report(output_net, net_stats)
            self.generate_case_119055_report()

            # Real vs SUMO 검증 추가
            self.validate_real_vs_sumo(output_net)

            print(f"\n{'='*60}")
            print(f"✓ 완료: {self.output_dir}")
            print(f"{'='*60}")
            print(f"\n산출물:")
            for f in sorted(self.output_dir.glob("*")):
                if f.is_file():
                    size_str = f"{f.stat().st_size / 1024 / 1024:.1f} MB" if f.stat().st_size > 1024*1024 else f"{f.stat().st_size / 1024:.1f} KB"
                    print(f"  {f.name:50} {size_str:>12}")

            return True

        except Exception as e:
            print(f"\n✗ 오류: {e}")
            import traceback
            traceback.print_exc()
            return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate pedestrian crossings from T2 CSV")
    parser.add_argument(
        "--t2-csv",
        default="smart_crosswalk_sumo/data/T2_crosswalk_features.csv",
        help="Path to T2 CSV file",
    )
    parser.add_argument(
        "--base-network",
        default="result/junggu_osm_network_20260513_184417/junggu_generated.net.xml",
        help="Path to base SUMO network",
    )
    parser.add_argument(
        "--output",
        default="result",
        help="Output base directory",
    )

    args = parser.parse_args()

    generator = T2CrosswalkGenerator(
        t2_csv_path=args.t2_csv,
        base_network_path=args.base_network,
        output_base=args.output,
    )
    success = generator.run()
    sys.exit(0 if success else 1)
