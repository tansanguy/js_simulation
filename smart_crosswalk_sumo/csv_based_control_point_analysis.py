"""
CSV 기반 횡단보도 제어점 분석 (MVP)

핵심: CSV 좌표를 source of truth로 삼아, SUMO 네트워크에서의 제어 가능성을 검증
기존 registry는 참고만 하고, current net.xml 기준으로 새로 계산

산출물: csv_control_points.csv (670행), summary md, 2개 map HTML, case_119055 md/html
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Dict, List, Tuple
import xml.etree.ElementTree as ET

import pandas as pd
import folium
from folium.plugins import MarkerCluster
from shapely.geometry import Point, LineString
from pyproj import Transformer

try:
    import sumolib
except ImportError:
    sumolib = None

try:
    from .output_schema import write_csv_utf8_sig
    from .preprocess import load_crosswalk_features
except ImportError:
    from output_schema import write_csv_utf8_sig
    from preprocess import load_crosswalk_features


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CSV_PATH = BASE_DIR / "data" / "T2_crosswalk_features.csv"
DEFAULT_NET_PATH = BASE_DIR.parent / "sumo_2d" / "net.xml"
DEFAULT_OUTPUT_DIR = BASE_DIR.parent / "result"

# 설정값
MAX_JUNCTION_SEARCH_RADIUS_M = 200
MAX_TLS_SEARCH_RADIUS_M = 300
CANDIDATE_EDGES_RADIUS_M = 50
EXISTING_CROSSING_THRESHOLD_M = 30
TLS_STRONG_CONTROLLABILITY_M = 75
TLS_WEAK_CONTROLLABILITY_M = 100
CASE_119055_NEIGHBORHOOD_RADIUS_M = 250

# 670개 기본 분석 대상 (기존 registry의 시뮬레이션 대상)
BASELINE_ANALYSIS_CANDIDATES = [
    10349, 10356, 136876, 136877, 119055,  # 이하 670개... (실제로는 기존 데이터에서 읽음)
    # NOTE: 전체 670개는 실제 분석 시 CSV에서 필터링
]


class CoordinateTransformer:
    """WGS84 ↔ SUMO projected 좌표 변환"""

    def __init__(self, net):
        """sumolib net object 사용"""
        self.net = net
        self.fallback_transformer = Transformer.from_crs("EPSG:4326", "EPSG:5179", always_xy=True)

    def wgs84_to_sumo(self, lon: float, lat: float) -> Tuple[Optional[float], Optional[float]]:
        """
        WGS84 (lon, lat) → SUMO projected (x, y)

        우선: net.convertLonLat2XY()
        fallback: pyproj
        """
        try:
            # sumolib 우선
            if self.net is not None:
                try:
                    x, y = self.net.convertLonLat2XY(lon, lat)
                    return float(x), float(y)
                except:
                    pass
        except:
            pass

        # pyproj fallback
        try:
            x, y = self.fallback_transformer.transform(lon, lat)
            return float(x), float(y)
        except:
            return None, None


class SUMONetworkAnalyzer:
    """SUMO net.xml 분석 및 쿼리"""

    def __init__(self, net):
        """sumolib net object"""
        self.net = net

    def find_nearest_junction(self, sumo_x: float, sumo_y: float) -> Tuple[Optional[str], Optional[float]]:
        """CSV 좌표에서 가장 가까운 junction 찾기"""
        if sumo_x is None or sumo_y is None:
            return None, None

        min_dist = float('inf')
        nearest_junc_id = None

        try:
            # sumolib: getNodes() returns junction objects
            for node in self.net.getNodes():
                node_x, node_y = node.getCoord()
                dist = math.sqrt((sumo_x - node_x)**2 + (sumo_y - node_y)**2)

                if dist < min_dist and dist <= MAX_JUNCTION_SEARCH_RADIUS_M:
                    min_dist = dist
                    nearest_junc_id = node.getID()
        except:
            pass

        if nearest_junc_id is None:
            return None, None
        return nearest_junc_id, min_dist

    def find_nearest_tls(self, sumo_x: float, sumo_y: float) -> Tuple[Optional[str], Optional[float]]:
        """CSV 좌표에서 가장 가까운 TLS 찾기"""
        if sumo_x is None or sumo_y is None:
            return None, None

        min_dist = float('inf')
        nearest_tls_id = None

        try:
            # sumolib: getTrafficLights() returns TLS ID strings
            for tls_id in self.net.getTrafficLights():
                # TLS는 junction과 associated
                node = self.net.getNode(tls_id)
                if node is None:
                    continue
                tls_x, tls_y = node.getCoord()
                dist = math.sqrt((sumo_x - tls_x)**2 + (sumo_y - tls_y)**2)

                if dist < min_dist and dist <= MAX_TLS_SEARCH_RADIUS_M:
                    min_dist = dist
                    nearest_tls_id = tls_id
        except:
            pass

        if nearest_tls_id is None:
            return None, None
        return nearest_tls_id, min_dist

    def find_candidate_edges(self, sumo_x: float, sumo_y: float, junction_id: str) -> List[Dict[str, Any]]:
        """junction 주변에서 candidate edges 찾기"""
        if sumo_x is None or sumo_y is None or junction_id is None:
            return []

        candidates = []
        point = Point(sumo_x, sumo_y)

        try:
            node = self.net.getNode(junction_id)
            if node is None:
                return []

            # incoming + outgoing edges
            edges_to_check = list(node.getIncoming()) + list(node.getOutgoing())

            for edge in edges_to_check:
                try:
                    # edge shape로부터 최단 거리 계산
                    shape = edge.getShape()
                    if len(shape) < 2:
                        continue

                    line = LineString(shape)
                    dist = point.distance(line)

                    if dist <= CANDIDATE_EDGES_RADIUS_M:
                        edge_id = edge.getID()
                        edge_type = 'crossing' if ':crossing' in edge_id else (
                            'incoming' if edge in node.getIncoming() else 'outgoing'
                        )
                        candidates.append({
                            'edge_id': edge_id,
                            'edge_type': edge_type,
                            'distance_m': dist
                        })
                except:
                    pass

            # 거리순 정렬
            candidates.sort(key=lambda x: x['distance_m'])

        except:
            pass

        return candidates

    def find_existing_sumo_crossing(self, sumo_x: float, sumo_y: float,
                                     candidate_edges: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[float]]:
        """CSV 좌표 주변의 실제 SUMO crossing edge 찾기"""
        for edge_info in candidate_edges:
            edge_id = edge_info['edge_id']
            if ':crossing' in edge_id or 'crossing:' in edge_id:
                return edge_id, edge_info['distance_m']

        return None, None

    def assess_tls_controllability(self, tls_id: Optional[str], junction_id: Optional[str]) -> Dict[str, Any]:
        """TLS 제어 가능성 검증"""
        result = {
            'tls_in_tllogic': False,
            'tls_in_connection': False,
            'has_pedestrian_phase': False,
            'pedestrian_phase_candidates': [],
            'can_control': False,
        }

        if tls_id is None:
            return result

        try:
            # tlLogic에 정의되어 있는가?
            tl_logic = self.net.getTLSSecure(tls_id)
            if tl_logic is not None:
                result['tls_in_tllogic'] = True

                # 보행자 phase 찾기
                for phase_idx, phase in enumerate(tl_logic.getPhases()):
                    state = phase.state
                    if 'w' in state.lower():
                        result['pedestrian_phase_candidates'].append(phase_idx)
                        result['has_pedestrian_phase'] = True

        except:
            pass

        # connection tl에 참조되는가? (간단한 확인)
        try:
            if tls_id in self.net.getConnections():
                result['tls_in_connection'] = True
        except:
            pass

        result['can_control'] = (result['tls_in_tllogic'] and result['has_pedestrian_phase'])

        return result


class ControlGradeAssessor:
    """Control grade 판정 (A/B/B_WEAK/C/D)"""

    @staticmethod
    def assess(csv_info: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        """
        Control grade 5단계 판정

        반환: (control_grade, failure_reason)
        """

        # D: 좌표 변환 실패 또는 junction 없음
        if csv_info['sumo_x'] is None or csv_info['nearest_junction_id'] is None:
            reason = 'coordinate_conversion_failed' if csv_info['sumo_x'] is None else 'no_junction_found'
            return 'D_INVALID_NETWORK_MAPPING', reason

        # A: 기존 crossing (< 30m) + TLS 제어 가능
        if (csv_info['existing_sumo_crossing_edge'] is not None and
            csv_info['existing_crossing_distance_m'] is not None and
            csv_info['existing_crossing_distance_m'] < EXISTING_CROSSING_THRESHOLD_M and
            csv_info['nearest_tls_id'] is not None and
            (csv_info['tls_in_tllogic'] or csv_info['tls_in_connection']) and
            csv_info['has_pedestrian_phase']):
            return 'A_REAL_SUMO_CROSSING', None

        # B: 새 crossing 생성 가능, TLS 제어 가능 (< 75m)
        if ((csv_info['existing_sumo_crossing_edge'] is None or
             csv_info['existing_crossing_distance_m'] >= EXISTING_CROSSING_THRESHOLD_M) and
            csv_info['nearest_tls_id'] is not None and
            csv_info['nearest_tls_distance_m'] is not None and
            csv_info['nearest_tls_distance_m'] < TLS_STRONG_CONTROLLABILITY_M and
            (csv_info['tls_in_tllogic'] or csv_info['tls_in_connection']) and
            csv_info['has_pedestrian_phase']):
            return 'B_CSV_SYNTHETIC_CONTROLLED', None

        # B_WEAK: TLS 있지만 거리 멀거나 phase 불명확
        if (csv_info['nearest_tls_id'] is not None and
            csv_info['nearest_tls_distance_m'] is not None and
            TLS_STRONG_CONTROLLABILITY_M <= csv_info['nearest_tls_distance_m'] < TLS_WEAK_CONTROLLABILITY_M and
            (csv_info['tls_in_tllogic'] or csv_info['tls_in_connection'])):
            return 'B_WEAK_SYNTHETIC_CONTROLLED', 'tls_too_far_or_phase_unclear'

        # C: 위치는 유효하나 TLS 제어 불가능
        if csv_info['nearest_junction_id'] is not None:
            if csv_info['nearest_tls_id'] is None:
                reason = 'no_tls_nearby'
            elif csv_info['nearest_tls_distance_m'] >= TLS_WEAK_CONTROLLABILITY_M:
                reason = 'tls_too_far'
            elif not csv_info['tls_in_tllogic'] and not csv_info['tls_in_connection']:
                reason = 'tls_not_in_network'
            elif not csv_info['has_pedestrian_phase']:
                reason = 'no_pedestrian_phase'
            else:
                reason = 'unknown'
            return 'C_CSV_LOCATION_ONLY', reason

        return 'D_INVALID_NETWORK_MAPPING', 'unknown_failure'


class CSVBasedControlPointAnalysis:
    """전체 분석 오케스트레이터"""

    def __init__(self, csv_path: str | Path = DEFAULT_CSV_PATH,
                 net_path: str | Path = DEFAULT_NET_PATH,
                 output_dir: str | Path = DEFAULT_OUTPUT_DIR):
        self.csv_path = Path(csv_path)
        self.net_path = Path(net_path)
        self.output_dir = Path(output_dir)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.result_dir = self.output_dir / f"csv_based_control_points_{timestamp}"
        self.result_dir.mkdir(parents=True, exist_ok=True)

        self.coordinate_transformer: Optional[CoordinateTransformer] = None
        self.network_analyzer: Optional[SUMONetworkAnalyzer] = None
        self.results_df: Optional[pd.DataFrame] = None
        self.csv_data: Optional[pd.DataFrame] = None
        self.net = None

    def load_data(self) -> None:
        """CSV, net.xml 로드"""
        print(f"Loading CSV from {self.csv_path}")
        self.csv_data = load_crosswalk_features(str(self.csv_path))
        print(f"  → {len(self.csv_data)} total crosswalks loaded")

        print(f"Loading SUMO network from {self.net_path}")
        if sumolib is not None:
            try:
                self.net = sumolib.net.readNet(str(self.net_path))
                print(f"  → sumolib loaded successfully")
            except Exception as e:
                print(f"  → sumolib failed: {e}, falling back...")
                self.net = None

        if self.net is None:
            # Fallback: basic ET parsing
            print(f"  → using basic XML parsing")
            self.net = ET.parse(str(self.net_path))

        self.network_analyzer = SUMONetworkAnalyzer(self.net)
        self.coordinate_transformer = CoordinateTransformer(self.net)

    def analyze_all_crosswalks(self) -> pd.DataFrame:
        """670개 횡단보도 분석"""
        print(f"Analyzing {len(self.csv_data)} crosswalks...")

        results = []
        for idx, (_, row) in enumerate(self.csv_data.iterrows()):
            if idx % 70 == 0:
                print(f"  [{idx}/{len(self.csv_data)}]")

            result = self._analyze_single_crosswalk(row)
            results.append(result)

        self.results_df = pd.DataFrame(results)
        return self.results_df

    def _analyze_single_crosswalk(self, row: pd.Series) -> Dict[str, Any]:
        """단일 횡단보도 분석"""
        crosswalk_id = int(row['crosswalk_id'])
        csv_lon, csv_lat = float(row['longitude']), float(row['latitude'])

        # [1] 좌표 변환
        sumo_x, sumo_y = self.coordinate_transformer.wgs84_to_sumo(csv_lon, csv_lat)

        # [2] Nearest junction
        nearest_junc_id, dist_to_junc = self.network_analyzer.find_nearest_junction(sumo_x, sumo_y)

        # [3] Nearest TLS
        nearest_tls_id, dist_to_tls = self.network_analyzer.find_nearest_tls(sumo_x, sumo_y)

        # [4] Candidate edges
        candidate_edges = []
        if nearest_junc_id and sumo_x is not None and sumo_y is not None:
            candidate_edges = self.network_analyzer.find_candidate_edges(sumo_x, sumo_y, nearest_junc_id)

        # [5] Existing SUMO crossing
        existing_crossing_id, dist_to_crossing = self.network_analyzer.find_existing_sumo_crossing(
            sumo_x, sumo_y, candidate_edges
        )

        # [6] TLS 제어 가능성
        tls_ctrl = self.network_analyzer.assess_tls_controllability(nearest_tls_id, nearest_junc_id)

        # [7] Control grade 판정
        csv_info = {
            'sumo_x': sumo_x, 'sumo_y': sumo_y,
            'nearest_junction_id': nearest_junc_id,
            'nearest_tls_id': nearest_tls_id,
            'nearest_tls_distance_m': dist_to_tls,
            'existing_sumo_crossing_edge': existing_crossing_id,
            'existing_crossing_distance_m': dist_to_crossing,
            **tls_ctrl,
        }
        control_grade, failure_reason = ControlGradeAssessor.assess(csv_info)

        return {
            'crosswalk_id': crosswalk_id,
            'csv_lon': csv_lon,
            'csv_lat': csv_lat,
            'sumo_x': sumo_x,
            'sumo_y': sumo_y,
            'nearest_junction_id': nearest_junc_id,
            'distance_to_junction_m': dist_to_junc,
            'nearest_tls_id': nearest_tls_id,
            'distance_to_tls_m': dist_to_tls,
            'nearest_edges': json.dumps([
                {'edge_id': e['edge_id'], 'type': e['edge_type'], 'distance': round(e['distance_m'], 2)}
                for e in candidate_edges[:3]
            ]),
            'existing_sumo_crossing_edge': existing_crossing_id,
            'existing_crossing_distance_m': dist_to_crossing,
            'control_grade': control_grade,
            'tls_in_tllogic': tls_ctrl['tls_in_tllogic'],
            'tls_in_connection': tls_ctrl['tls_in_connection'],
            'pedestrian_phase_candidate': json.dumps(tls_ctrl['pedestrian_phase_candidates']),
            'failure_reason': failure_reason,
            'notes': '',
        }

    def generate_summary_report(self) -> str:
        """전체 요약 보고서 생성"""
        grade_counts = self.results_df['control_grade'].value_counts()

        summary = f"""# CSV 기반 횡단보도 제어점 분석 결과

## 분석 정보

- **실행 날짜**: {datetime.now().strftime("%Y-%m-%d")}
- **실행 시간**: {datetime.now().strftime("%H:%M:%S")} KST
- **CSV 데이터**: T2_crosswalk_features.csv ({len(self.results_df)}개 횡단보도)
- **SUMO 네트워크**: sumo_2d/net.xml

## 등급별 집계

| 등급 | 개수 | 비율 | 설명 |
|------|------|------|------|
"""

        total = len(self.results_df)
        for grade in ['A_REAL_SUMO_CROSSING', 'B_CSV_SYNTHETIC_CONTROLLED', 'B_WEAK_SYNTHETIC_CONTROLLED',
                      'C_CSV_LOCATION_ONLY', 'D_INVALID_NETWORK_MAPPING']:
            count = grade_counts.get(grade, 0)
            pct = 100 * count / total if total > 0 else 0
            grade_desc = {
                'A_REAL_SUMO_CROSSING': '기존 SUMO crossing + TLS 제어 가능',
                'B_CSV_SYNTHETIC_CONTROLLED': '새 crossing 생성 + TLS 제어 가능',
                'B_WEAK_SYNTHETIC_CONTROLLED': 'TLS 있으나 거리 멀거나 phase 불명확',
                'C_CSV_LOCATION_ONLY': '위치 유효하나 TLS 제어 불가능',
                'D_INVALID_NETWORK_MAPPING': '네트워크 연결 실패',
            }
            summary += f"| {grade} | {count} | {pct:.1f}% | {grade_desc[grade]} |\n"

        summary += f"| **합계** | **{total}** | **100%** | |\n\n"

        summary += """## 시뮬레이션 대상 후보

### 권장 대상 (A + B)

"""

        ab_count = grade_counts.get('A_REAL_SUMO_CROSSING', 0) + grade_counts.get('B_CSV_SYNTHETIC_CONTROLLED', 0)
        ab_pct = 100 * ab_count / total if total > 0 else 0
        summary += f"- **개수**: {ab_count}개 ({ab_pct:.1f}%)\n"
        summary += f"- **상태**: 즉시 시뮬레이션 진행 가능\n\n"

        summary += """## 다음 단계

1. A + B 등급 후보 선정 및 시뮬레이션 진행
2. B_WEAK 등급에 대한 상세 검토
3. C 등급: 향후 TLS 추가/수정 후 재분석
4. D 등급: 네트워크 재검토

"""

        return summary

    def generate_interactive_map(self) -> None:
        """대화형 지도 생성"""
        m = folium.Map(
            location=[37.56, 126.98],  # 중구 중심
            zoom_start=12,
            tiles='OpenStreetMap'
        )

        # 색상 매핑
        color_map = {
            'A_REAL_SUMO_CROSSING': 'green',
            'B_CSV_SYNTHETIC_CONTROLLED': 'blue',
            'B_WEAK_SYNTHETIC_CONTROLLED': 'lightblue',
            'C_CSV_LOCATION_ONLY': 'orange',
            'D_INVALID_NETWORK_MAPPING': 'red',
        }

        # 마커 클러스터링
        marker_cluster = MarkerCluster().add_to(m)

        for _, row in self.results_df.iterrows():
            lat, lon = row['csv_lat'], row['csv_lon']
            grade = row['control_grade']
            color = color_map.get(grade, 'gray')

            tls_dist = f"{row['distance_to_tls_m']:.1f}m" if row['distance_to_tls_m'] is not None else 'N/A'
            popup_text = f"""
            <b>ID: {row['crosswalk_id']}</b><br>
            Grade: {grade}<br>
            TLS: {row['nearest_tls_id'] if row['nearest_tls_id'] else 'None'}<br>
            Distance to TLS: {tls_dist}
            """

            folium.Marker(
                location=[lat, lon],
                popup=folium.Popup(popup_text, max_width=300),
                icon=folium.Icon(color=color, icon='info-sign'),
            ).add_to(marker_cluster)

        # 범례
        legend_html = '''
        <div style="position: fixed;
             bottom: 50px; right: 50px; width: 250px; height: 200px;
             background-color: white; border:2px solid grey; z-index:9999;
             font-size:12px; padding:10px">
        <p style="margin: 0;"><b>Control Grade</b></p>
        <p style="margin: 5px 0;"><i class="fa fa-map-marker" style="color:green"></i> A_REAL_SUMO_CROSSING</p>
        <p style="margin: 5px 0;"><i class="fa fa-map-marker" style="color:blue"></i> B_CSV_SYNTHETIC_CONTROLLED</p>
        <p style="margin: 5px 0;"><i class="fa fa-map-marker" style="color:lightblue"></i> B_WEAK_SYNTHETIC_CONTROLLED</p>
        <p style="margin: 5px 0;"><i class="fa fa-map-marker" style="color:orange"></i> C_CSV_LOCATION_ONLY</p>
        <p style="margin: 5px 0;"><i class="fa fa-map-marker" style="color:red"></i> D_INVALID_NETWORK_MAPPING</p>
        </div>
        '''
        m.get_root().html.add_child(folium.Element(legend_html))

        map_path = self.result_dir / "csv_control_points_map.html"
        m.save(str(map_path))
        print(f"Saved map: {map_path}")

    def analyze_case_119055(self) -> Dict[str, Any]:
        """case 119055 상세 분석"""
        row = self.results_df[self.results_df['crosswalk_id'] == 119055]
        if row.empty:
            print("WARNING: case 119055 not found in results")
            return {}

        row = row.iloc[0]

        # B_CSV_SYNTHETIC_CONTROLLED 가능 여부
        b_possible = row['control_grade'] in ['B_CSV_SYNTHETIC_CONTROLLED', 'A_REAL_SUMO_CROSSING']

        # 최종 권고
        if row['control_grade'] == 'A_REAL_SUMO_CROSSING':
            recommendation = 'USE_AS_PILOT'
        elif row['control_grade'] == 'B_CSV_SYNTHETIC_CONTROLLED':
            recommendation = 'USE_AS_PILOT'
        elif row['control_grade'] == 'B_WEAK_SYNTHETIC_CONTROLLED':
            recommendation = 'CONDITIONAL_PILOT'
        else:
            recommendation = 'DO_NOT_USE'

        junc_dist = f"{row['distance_to_junction_m']:.1f}m" if row['distance_to_junction_m'] is not None else 'N/A'
        tls_dist = f"{row['distance_to_tls_m']:.1f}m" if row['distance_to_tls_m'] is not None else 'N/A'
        cross_dist = f"{row['existing_crossing_distance_m']:.1f}m" if row['existing_crossing_distance_m'] is not None else 'N/A'
        sumo_x_str = f"{row['sumo_x']:.2f}" if row['sumo_x'] is not None else 'N/A'
        sumo_y_str = f"{row['sumo_y']:.2f}" if row['sumo_y'] is not None else 'N/A'

        report = f"""# Case 119055: 순화동 횡단보도 제어점 분석

## 기본 정보

| 항목 | 값 |
|------|-----|
| Crosswalk ID | 119055 |
| 위치 | 순화동, 을지로동 |
| CSV 좌표 (WGS84) | ({row['csv_lon']:.6f}, {row['csv_lat']:.6f}) |
| SUMO 좌표 | ({sumo_x_str}, {sumo_y_str}) |

## 분석 결과

| 항목 | 값 |
|------|-----|
| Nearest Junction | {row['nearest_junction_id']} ({junc_dist}) |
| Nearest TLS | {row['nearest_tls_id']} ({tls_dist}) |
| TLS in tlLogic | {row['tls_in_tllogic']} |
| TLS in Connection | {row['tls_in_connection']} |
| Existing Crossing | {row['existing_sumo_crossing_edge'] if row['existing_sumo_crossing_edge'] else 'None'} |
| Existing Crossing Distance | {cross_dist} |
| Pedestrian Phase Candidate | {row['pedestrian_phase_candidate']} |

## 제어 등급 판정

**Control Grade**: `{row['control_grade']}`

**B_CSV_SYNTHETIC_CONTROLLED 가능**: `{b_possible}`

**최종 권고**: `{recommendation}`

---
분석 완료: {datetime.now().isoformat()}
"""

        return {
            'report': report,
            'b_possible': b_possible,
            'recommendation': recommendation,
            'row': row,
        }

    def generate_case_119055_neighborhood_map(self) -> None:
        """119055 주변 지도 생성"""
        row_119055 = self.results_df[self.results_df['crosswalk_id'] == 119055]
        if row_119055.empty:
            print("WARNING: case 119055 not found for neighborhood map")
            return

        row_119055 = row_119055.iloc[0]
        center_lat, center_lon = row_119055['csv_lat'], row_119055['csv_lon']

        # 반경 250m 내 후보 필터링
        m = folium.Map(
            location=[center_lat, center_lon],
            zoom_start=15,
            tiles='OpenStreetMap'
        )

        color_map = {
            'A_REAL_SUMO_CROSSING': 'green',
            'B_CSV_SYNTHETIC_CONTROLLED': 'blue',
            'B_WEAK_SYNTHETIC_CONTROLLED': 'lightblue',
            'C_CSV_LOCATION_ONLY': 'orange',
            'D_INVALID_NETWORK_MAPPING': 'red',
        }

        # 250m 반경 원 표시
        folium.Circle(
            location=[center_lat, center_lon],
            radius=CASE_119055_NEIGHBORHOOD_RADIUS_M,
            color='gray',
            fill=False,
            weight=2,
        ).add_to(m)

        # 119055 자신 (굵은 마커)
        folium.Marker(
            location=[center_lat, center_lon],
            popup=folium.Popup(f"<b>★ Crosswalk 119055 (주인공)</b><br>Grade: {row_119055['control_grade']}", max_width=300),
            icon=folium.Icon(color='yellow', icon='star', prefix='fa'),
        ).add_to(m)

        # 주변 후보 (반경 250m 내)
        for _, row in self.results_df.iterrows():
            if row['crosswalk_id'] == 119055:
                continue

            lat, lon = row['csv_lat'], row['csv_lon']
            dist = math.sqrt((lat - center_lat)**2 + (lon - center_lon)**2) * 111000  # 대략적 m 단위

            if dist <= CASE_119055_NEIGHBORHOOD_RADIUS_M:
                grade = row['control_grade']
                color = color_map.get(grade, 'gray')

                popup_text = f"""
                <b>ID: {row['crosswalk_id']}</b><br>
                Grade: {grade}<br>
                Distance from 119055: {dist:.1f}m
                """

                folium.Marker(
                    location=[lat, lon],
                    popup=folium.Popup(popup_text, max_width=300),
                    icon=folium.Icon(color=color, icon='info-sign'),
                ).add_to(m)

        # 범례
        legend_html = '''
        <div style="position: fixed;
             bottom: 50px; right: 50px; width: 250px; height: 220px;
             background-color: white; border:2px solid grey; z-index:9999;
             font-size:12px; padding:10px">
        <p style="margin: 0;"><b>Control Grade (250m radius)</b></p>
        <p style="margin: 5px 0;"><i class="fa fa-star" style="color:gold"></i> 119055 (주인공)</p>
        <p style="margin: 5px 0;"><i class="fa fa-map-marker" style="color:green"></i> A_REAL_SUMO_CROSSING</p>
        <p style="margin: 5px 0;"><i class="fa fa-map-marker" style="color:blue"></i> B_CSV_SYNTHETIC_CONTROLLED</p>
        <p style="margin: 5px 0;"><i class="fa fa-map-marker" style="color:lightblue"></i> B_WEAK_SYNTHETIC_CONTROLLED</p>
        <p style="margin: 5px 0;"><i class="fa fa-map-marker" style="color:orange"></i> C_CSV_LOCATION_ONLY</p>
        <p style="margin: 5px 0;"><i class="fa fa-map-marker" style="color:red"></i> D_INVALID_NETWORK_MAPPING</p>
        </div>
        '''
        m.get_root().html.add_child(folium.Element(legend_html))

        map_path = self.result_dir / "case_119055_neighborhood_map.html"
        m.save(str(map_path))
        print(f"Saved neighborhood map: {map_path}")

    def export_results(self) -> None:
        """모든 산출물 생성"""
        if self.results_df is None:
            raise ValueError("No analysis results. Call analyze_all_crosswalks() first.")

        # [1] CSV
        csv_output = self.result_dir / "csv_control_points.csv"
        write_csv_utf8_sig(self.results_df, str(csv_output))
        print(f"Saved CSV: {csv_output} ({len(self.results_df)} rows)")

        # [2] Summary report
        summary_report = self.generate_summary_report()
        summary_path = self.result_dir / "csv_control_point_summary.md"
        summary_path.write_text(summary_report, encoding='utf-8')
        print(f"Saved summary: {summary_path}")

        # [3] Main map
        self.generate_interactive_map()

        # [4] Case 119055
        case_result = self.analyze_case_119055()
        if case_result:
            case_path = self.result_dir / "case_119055_control_point.md"
            case_path.write_text(case_result['report'], encoding='utf-8')
            print(f"Saved case 119055: {case_path}")

        # [5] Case 119055 neighborhood map
        self.generate_case_119055_neighborhood_map()

    def run(self) -> None:
        """전체 파이프라인"""
        print("=" * 80)
        print("CSV-Based Crosswalk Control Point Analysis (MVP)")
        print("=" * 80)

        self.load_data()
        self.analyze_all_crosswalks()
        self.export_results()

        print("=" * 80)
        print(f"Results saved to: {self.result_dir}")
        print("=" * 80)

        # 최종 요약
        grade_counts = self.results_df['control_grade'].value_counts()
        case_119055 = self.results_df[self.results_df['crosswalk_id'] == 119055]

        print("\n[Summary]")
        print(f"Total analyzed: {len(self.results_df)} crosswalks")
        print(f"Control grade distribution:")
        for grade in ['A_REAL_SUMO_CROSSING', 'B_CSV_SYNTHETIC_CONTROLLED', 'B_WEAK_SYNTHETIC_CONTROLLED',
                      'C_CSV_LOCATION_ONLY', 'D_INVALID_NETWORK_MAPPING']:
            count = grade_counts.get(grade, 0)
            print(f"  {grade}: {count}")

        if not case_119055.empty:
            row = case_119055.iloc[0]
            tls_dist = f"{row['distance_to_tls_m']:.1f}m" if row['distance_to_tls_m'] is not None else 'N/A'
            print(f"\nCase 119055:")
            print(f"  Control grade: {row['control_grade']}")
            print(f"  Nearest TLS: {row['nearest_tls_id']} ({tls_dist})")
            print(f"  B_CSV_SYNTHETIC_CONTROLLED possible: {row['control_grade'] in ['B_CSV_SYNTHETIC_CONTROLLED', 'A_REAL_SUMO_CROSSING']}")

        print(f"\nCurrent net.xml TLS count: {len(self.net.getTrafficLights()) if hasattr(self.net, 'getTrafficLights') else 'N/A'}")


def main():
    """메인 진입점"""
    import argparse

    parser = argparse.ArgumentParser(description="CSV-based Crosswalk Control Point Analysis (MVP)")
    parser.add_argument("--csv", type=str, default=str(DEFAULT_CSV_PATH), help="T2 CSV path")
    parser.add_argument("--net", type=str, default=str(DEFAULT_NET_PATH), help="SUMO net.xml path")
    parser.add_argument("--output", type=str, default=str(DEFAULT_OUTPUT_DIR), help="Output directory")

    args = parser.parse_args()

    analysis = CSVBasedControlPointAnalysis(
        csv_path=args.csv,
        net_path=args.net,
        output_dir=args.output
    )
    analysis.run()


if __name__ == "__main__":
    main()
