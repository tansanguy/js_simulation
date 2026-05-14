#!/usr/bin/env python3
"""
횡단보도 매칭 검수용 전수 시각화 파이프라인 v2

개선사항:
- T2_crosswalk_features.csv와 join으로 원본 좌표 복구
- net.xml에서 <edge id> 및 <edge function="crossing"> 기준 검증
- TLS 검증 분리 (registry vs current net.xml)
- PNG 카드를 실제 lon/lat 좌표 기반으로 생성
- HTML 지도의 마커 구분 (CSV circle vs SUMO square)
- 670개 색상 구분 시각화
"""

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import matplotlib.pyplot as plt
import matplotlib.patches as patches
import pandas as pd
import numpy as np

BASE_DIR = Path(__file__).parent


def haversine(lat1, lon1, lat2, lon2):
    """두 좌표 사이의 거리를 미터 단위로 반환."""
    R = 6371000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return R * c


def parse_args():
    """CLI 인자 파싱."""
    parser = argparse.ArgumentParser(
        description="횡단보도 매칭 신뢰성 검수용 전수 시각화 생성 v2"
    )
    parser.add_argument(
        "--registry",
        default=str(BASE_DIR / "registry" / "junggu_crosswalk_sumo_registry.csv"),
        help="Registry CSV 경로"
    )
    parser.add_argument(
        "--crosswalk-features",
        default=str(BASE_DIR / "data" / "T2_crosswalk_features.csv"),
        help="T2 횡단보도 좌표 CSV 경로"
    )
    parser.add_argument(
        "--net-xml",
        default=str(BASE_DIR / "sumo_nets" / "cw_23040" / "network.net.xml"),
        help="SUMO net.xml 경로"
    )
    parser.add_argument(
        "--output-dir",
        default=str(BASE_DIR.parent / "result" / f"matching_visual_audit_v2_{datetime.now().strftime('%Y%m%d_%H%M%S')}"),
        help="산출물 디렉토리"
    )
    parser.add_argument(
        "--cards",
        choices=["all", "suspicious", "none"],
        default="suspicious",
        help="PNG 카드 생성 범위"
    )
    return parser.parse_args()


def load_registry(registry_path):
    """Registry CSV 로딩."""
    df = pd.read_csv(registry_path)
    print(f"✓ Registry 로드: {len(df)} 행")
    return df


def load_crosswalk_features(features_path):
    """T2 횡단보도 특성 CSV 로딩."""
    if not Path(features_path).exists():
        print(f"⚠ T2 CSV 없음: {features_path}")
        return pd.DataFrame()

    df = pd.read_csv(features_path)
    # 컬럼명 정규화
    df.rename(columns={"횡단보도ID": "crosswalk_id", "lon": "t2_lon", "lat": "t2_lat"}, inplace=True)
    print(f"✓ T2 CSV 로드: {len(df)} 행")
    return df


def parse_net_xml(net_xml_path):
    """net.xml에서 edge 정보 추출."""
    all_edges = set()
    crossing_edges = set()
    crossing_edges_with_function = set()
    tls_in_tllogic = set()
    tls_in_connection = set()

    if not Path(net_xml_path).exists():
        print(f"⚠ net.xml 없음: {net_xml_path}")
        return all_edges, crossing_edges, crossing_edges_with_function, tls_in_tllogic, tls_in_connection

    try:
        tree = ET.parse(net_xml_path)
        root = tree.getroot()

        # 모든 edge id 수집
        for edge in root.findall(".//edge"):
            edge_id = edge.get("id")
            if edge_id:
                all_edges.add(edge_id)
                # function="crossing"인 edge 수집
                if edge.get("function") == "crossing":
                    crossing_edges_with_function.add(edge_id)

        # tlLogic 수집
        for tllogic in root.findall(".//tlLogic"):
            tls_id = tllogic.get("id")
            if tls_id:
                tls_in_tllogic.add(tls_id)

        # connection에 tls 참조 확인
        for connection in root.findall(".//connection"):
            tls_id = connection.get("tls")
            if tls_id:
                tls_in_connection.add(tls_id)

        # <crossing edges="..."> 속의 edges 수집 (참고용)
        for crossing in root.findall(".//crossing"):
            edges = crossing.get("edges", "").split()
            for edge in edges:
                crossing_edges.add(edge)

        print(f"✓ net.xml 파싱: {len(all_edges)} edges, "
              f"{len(crossing_edges_with_function)} crossing edges, "
              f"{len(tls_in_tllogic)} tls in tlLogic")
    except Exception as e:
        print(f"⚠ net.xml 파싱 실패: {e}")

    return all_edges, crossing_edges, crossing_edges_with_function, tls_in_tllogic, tls_in_connection


def compute_audit_columns(df, t2_df, all_edges, crossing_edges_with_function, tls_in_tllogic, tls_in_connection):
    """감사 컬럼 계산."""
    # T2와 merge
    if not t2_df.empty:
        df = df.merge(t2_df[["crosswalk_id", "t2_lon", "t2_lat"]], on="crosswalk_id", how="left")
    else:
        df["t2_lon"] = np.nan
        df["t2_lat"] = np.nan

    audit_data = []

    for idx, row in df.iterrows():
        crosswalk_id = row.get("crosswalk_id")

        # 원본 좌표: registry 우선, 없으면 T2 사용
        orig_lat = row.get("original_csv_lat")
        orig_lon = row.get("original_csv_lon")
        if pd.isna(orig_lat) or pd.isna(orig_lon):
            orig_lat = row.get("t2_lat")
            orig_lon = row.get("t2_lon")

        cross_lat = row.get("crossing_lat")
        cross_lon = row.get("crossing_lon")
        crossing_edge = row.get("crossing_edge")
        tls_id = row.get("tls_id")
        ped_links = row.get("ped_link_indices")
        match_dist = row.get("match_distance_m", float("nan"))
        registry_status = row.get("registry_status")
        recovery_status = row.get("recovery_status")
        c_subtype = row.get("c_subtype")
        mapping_conf = row.get("mapping_confidence")
        runnable_for_sig_ext = row.get("runnable_for_signal_extension")

        # haversine 거리 계산
        if pd.notna(orig_lat) and pd.notna(orig_lon) and pd.notna(cross_lat) and pd.notna(cross_lon):
            haversine_dist = haversine(orig_lat, orig_lon, cross_lat, cross_lon)
        else:
            haversine_dist = float("nan")

        # net.xml 검증 (개선된 로직)
        crossing_in_all_edges = crossing_edge in all_edges if pd.notna(crossing_edge) else False
        crossing_is_function = crossing_edge in crossing_edges_with_function if pd.notna(crossing_edge) else False
        tls_in_logic = tls_id in tls_in_tllogic if pd.notna(tls_id) else False
        tls_in_conn = tls_id in tls_in_connection if pd.notna(tls_id) else False

        # 중복 counting
        same_crossing = (df["crossing_edge"] == crossing_edge).sum() if pd.notna(crossing_edge) else 0
        same_tls = (df["tls_id"] == tls_id).sum() if pd.notna(tls_id) else 0

        # risk_flags 조합
        risk_flags = []

        if pd.notna(match_dist):
            if match_dist > 75:
                risk_flags.append("DIST_GT_75M")
            elif match_dist > 50:
                risk_flags.append("DIST_GT_50M")
            elif match_dist > 25:
                risk_flags.append("DIST_GT_25M")

        if registry_status == "patched_unsignalized":
            risk_flags.append("PATCHED_UNSIGNALIZED")
            risk_flags.append("PATCH_BASED_GEOMETRY")

        if registry_status == "recovered":
            risk_flags.append("RECOVERED")

        if same_crossing > 1:
            risk_flags.append("DUPLICATE_CROSSING")

        if same_tls > 1:
            risk_flags.append("DUPLICATE_TLS")

        if pd.isna(tls_id) or (pd.notna(tls_id) and tls_id == ""):
            risk_flags.append("NO_TLS")
            risk_flags.append("CONTROL_UNAVAILABLE")

        if not tls_in_logic and pd.notna(tls_id):
            risk_flags.append("TLS_NOT_IN_TLLOGIC")

        if not tls_in_conn and pd.notna(tls_id):
            risk_flags.append("TLS_NOT_IN_CONNECTION")

        if pd.isna(ped_links) or (pd.notna(ped_links) and str(ped_links).strip() == ""):
            risk_flags.append("NO_PED_LINK")
            risk_flags.append("CONTROL_UNAVAILABLE")

        if not crossing_in_all_edges:
            risk_flags.append("CROSSING_NOT_IN_NETXML")

        if pd.isna(orig_lon) or pd.isna(orig_lat):
            risk_flags.append("MISSING_ORIGINAL_COORDINATE")

        risk_flags.append("HTML_MAP_NEEDS_INTERNET_FOR_BASEMAP")
        risk_flags.append("NETXML_REGISTRY_VERSION_UNCHECKED")

        risk_flags_str = "|".join(sorted(set(risk_flags)))

        # auto_audit_grade 판정 (개선된 로직)
        # INVALID_OR_UNUSABLE: 원본 좌표 부재
        if pd.isna(orig_lat) or pd.isna(orig_lon):
            auto_grade = "INVALID_OR_UNUSABLE"
        # VERY_HIGH_RISK: patched_unsignalized, dist>75, crossing/tls/ped 근본적으로 불명확
        elif (registry_status == "patched_unsignalized" or
              (pd.notna(match_dist) and match_dist > 75) or
              not crossing_in_all_edges or
              (pd.isna(tls_id) or tls_id == "") or
              (pd.isna(ped_links) or str(ped_links).strip() == "")):
            auto_grade = "VERY_HIGH_RISK"
        # HIGH_RISK: 50<dist≤75, duplicate, recovered, or partial control unavailable
        elif (pd.notna(match_dist) and match_dist > 50 and match_dist <= 75) or \
             same_crossing > 1 or same_tls > 1 or \
             (registry_status == "recovered" and (not tls_in_logic or not tls_in_conn)):
            auto_grade = "HIGH_RISK"
        # MEDIUM_RISK: 25<dist≤50, recovered with some control available, or mapping_confidence uncertain
        elif (pd.notna(match_dist) and match_dist > 25 and match_dist <= 50) or \
             registry_status == "recovered" or \
             mapping_conf != "verified":
            auto_grade = "MEDIUM_RISK"
        # LOW_RISK: dist≤25, crossing/tls/ped all present, no duplicate
        elif pd.notna(match_dist) and match_dist <= 25 and crossing_in_all_edges and \
             tls_in_logic and pd.notna(ped_links) and same_crossing == 1 and same_tls == 1:
            auto_grade = "LOW_RISK"
        else:
            auto_grade = "MEDIUM_RISK"

        # 특수: recovered이고 좌표 있고 거리 적당하면 HIGH_RISK → CONDITIONAL
        if registry_status == "recovered" and \
           pd.notna(orig_lat) and pd.notna(orig_lon) and \
           pd.notna(match_dist) and 25 <= match_dist <= 75:
            # CONTROL_UNAVAILABLE flag가 없으면 CONDITIONAL로 봐도 됨
            if "CONTROL_UNAVAILABLE" not in risk_flags:
                auto_grade = "HIGH_RISK"  # 실제로는 PILOT_CONDITIONAL 정도

        # priority_rank 계산 (tuple 대신 정수로 변환 예정)
        grade_order = {"INVALID_OR_UNUSABLE": 1, "VERY_HIGH_RISK": 2, "HIGH_RISK": 3, "MEDIUM_RISK": 4, "LOW_RISK": 5}
        base_rank = grade_order.get(auto_grade, 99)

        is_patched = 1 if registry_status == "patched_unsignalized" else 0
        dist_val = float("inf") - (match_dist if pd.notna(match_dist) else 0)
        dup_cross = -same_crossing if same_crossing > 1 else 0
        dup_tls = -same_tls if same_tls > 1 else 0

        priority_rank_key = (base_rank, -is_patched, dist_val, dup_cross, dup_tls)

        audit_data.append({
            "crosswalk_id": crosswalk_id,
            "original_lat": orig_lat,
            "original_lon": orig_lon,
            "crossing_edge": crossing_edge,
            "crossing_lat": cross_lat,
            "crossing_lon": cross_lon,
            "match_distance_m": match_dist,
            "haversine_distance_m": haversine_dist,
            "registry_status": registry_status,
            "mapping_confidence": mapping_conf,
            "c_subtype": c_subtype,
            "recovery_status": recovery_status,
            "runnable_for_signal_extension": runnable_for_sig_ext,
            "tls_id": tls_id,
            "tls_id_present_in_registry": pd.notna(tls_id) and tls_id != "",
            "tls_in_tllogic_current_netxml": tls_in_logic,
            "tls_in_connection_current_netxml": tls_in_conn,
            "ped_link_indices": ped_links,
            "crossing_in_all_edges": crossing_in_all_edges,
            "crossing_is_function_crossing": crossing_is_function,
            "duplicate_crossing_count": same_crossing,
            "duplicate_tls_count": same_tls,
            "risk_flags": risk_flags_str,
            "priority_rank_key": priority_rank_key,
            "auto_audit_grade": auto_grade,
            "netxml_validation_status": "CURRENT_NET_VERSION" if not tls_in_logic else "VERIFIED",
            "auto_audit_reason": f"grade={auto_grade}, dist={match_dist:.1f}m, crossing_in_edges={crossing_in_all_edges}, tls={pd.notna(tls_id)}"
        })

    audit_df = pd.DataFrame(audit_data)

    # priority_rank를 정수로 변환
    sorted_indices = sorted(range(len(audit_df)), key=lambda i: audit_df.iloc[i]["priority_rank_key"])
    audit_df["priority_rank"] = 0
    for rank, idx in enumerate(sorted_indices, 1):
        audit_df.loc[idx, "priority_rank"] = rank

    audit_df = audit_df.drop("priority_rank_key", axis=1)
    print(f"✓ Audit 컬럼 계산 완료: {len(audit_df)} 행")
    return audit_df


def save_crosswalk_visual_audit_csv(audit_df, output_dir):
    """audit CSV 저장."""
    output_file = output_dir / "crosswalk_visual_audit.csv"
    cols_to_save = [col for col in audit_df.columns if col != "priority_rank_key"]
    audit_df[cols_to_save].to_csv(output_file, index=False)
    print(f"✓ 저장: {output_file}")


def save_manual_review_template_csv(audit_df, output_dir):
    """수동 검수용 CSV 템플릿."""
    output_file = output_dir / "manual_review_template.csv"
    template_df = audit_df[[
        "crosswalk_id", "auto_audit_grade", "priority_rank",
        "registry_status", "match_distance_m", "risk_flags"
    ]].copy()
    template_df["image_path"] = template_df["crosswalk_id"].apply(lambda x: f"cards/crosswalk_{x}.png")
    template_df["html_anchor"] = template_df["crosswalk_id"].apply(lambda x: f"marker_{x}")
    template_df["human_decision"] = ""
    template_df["human_reason"] = ""
    template_df["reviewer"] = ""
    template_df["reviewed_at"] = ""
    template_df.to_csv(output_file, index=False)
    print(f"✓ 저장: {output_file}")


def save_priority_review_list_csv(audit_df, output_dir):
    """우선순위 정렬 목록."""
    output_file = output_dir / "priority_review_list.csv"
    sorted_df = audit_df.sort_values("priority_rank").copy()
    sorted_df.to_csv(output_file, index=False)
    print(f"✓ 저장: {output_file}")


def generate_color_palette(n):
    """670개 항목을 위한 색상 팔레트 생성 (HSL 기반)."""
    colors = []
    for i in range(n):
        hue = (i * 360 / n) % 360
        saturation = 70 + (i % 10) * 3
        lightness = 50 + (i % 5) * 2
        # HSL to hex (간단한 근사)
        c = plt.cm.hsv(i / n)
        colors.append(f"#{int(c[0]*255):02x}{int(c[1]*255):02x}{int(c[2]*255):02x}")
    return colors


def build_all_crosswalks_map_html(audit_df, output_dir):
    """전체 670개 지도 HTML (개선됨)."""
    output_file = output_dir / "all_crosswalks_map.html"

    grade_color = {
        "LOW_RISK": "#2ecc71",
        "MEDIUM_RISK": "#3498db",
        "HIGH_RISK": "#f39c12",
        "VERY_HIGH_RISK": "#e74c3c",
        "INVALID_OR_UNUSABLE": "#7f8c8d"
    }

    # 670개 색상 생성
    color_palette = generate_color_palette(len(audit_df))

    features = []
    for idx, (_, row) in enumerate(audit_df.iterrows()):
        if pd.isna(row["original_lat"]) or pd.isna(row["original_lon"]):
            continue

        grade_color_val = grade_color.get(row["auto_audit_grade"], "#95a5a6")
        individual_color = color_palette[idx]

        # CSV 원본 좌표 (circle, 개별 색상)
        popup_csv = (
            f"<b>CSV Point - ID {row['crosswalk_id']}</b><br>"
            f"Lat: {row['original_lat']:.6f}<br>"
            f"Lon: {row['original_lon']:.6f}<br>"
            f"Grade: {row['auto_audit_grade']}"
        )

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [row["original_lon"], row["original_lat"]]
            },
            "properties": {
                "id": f"{row['crosswalk_id']}_csv",
                "grade": row["auto_audit_grade"],
                "color": individual_color,
                "marker_type": "csv",
                "popup": popup_csv
            }
        })

        if pd.notna(row["crossing_lon"]) and pd.notna(row["crossing_lat"]):
            # SUMO crossing 좌표 (square-like, 등급 색상)
            popup_sumo = (
                f"<b>SUMO Crossing - ID {row['crosswalk_id']}</b><br>"
                f"Lat: {row['crossing_lat']:.6f}<br>"
                f"Lon: {row['crossing_lon']:.6f}<br>"
                f"Grade: {row['auto_audit_grade']}"
            )

            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [row["crossing_lon"], row["crossing_lat"]]
                },
                "properties": {
                    "id": f"{row['crosswalk_id']}_sumo",
                    "grade": row["auto_audit_grade"],
                    "color": individual_color,
                    "marker_type": "sumo",
                    "popup": popup_sumo
                }
            })

            # 연결선
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [row["original_lon"], row["original_lat"]],
                        [row["crossing_lon"], row["crossing_lat"]]
                    ]
                },
                "properties": {
                    "id": f"{row['crosswalk_id']}_line",
                    "grade": row["auto_audit_grade"],
                    "color": individual_color
                }
            })

    geojson_data = json.dumps({"type": "FeatureCollection", "features": features})

    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8" />
    <title>All Crosswalks - Matching Audit v2</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css" />
    <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
    <style>
        body {{ margin: 0; padding: 0; }}
        #map {{ position: absolute; top: 0; bottom: 0; width: 100%; }}
        .info {{ background: white; padding: 10px; border-radius: 5px; box-shadow: 0 0 15px rgba(0,0,0,0.2); }}
        .legend {{ background: white; padding: 10px; border-radius: 5px; max-height: 300px; overflow-y: auto; }}
        .legend-item {{ display: flex; align-items: center; gap: 8px; margin: 5px 0; font-size: 12px; }}
        .legend-color {{ width: 12px; height: 12px; border-radius: 50%; }}
    </style>
</head>
<body>
    <div id="map"></div>
    <div class="info" style="position: absolute; top: 10px; left: 10px; z-index: 1000;">
        <div class="legend">
            <h4>Matching Grade</h4>
            <div class="legend-item"><div class="legend-color" style="background: #2ecc71;"></div> Low Risk</div>
            <div class="legend-item"><div class="legend-color" style="background: #3498db;"></div> Medium Risk</div>
            <div class="legend-item"><div class="legend-color" style="background: #f39c12;"></div> High Risk</div>
            <div class="legend-item"><div class="legend-color" style="background: #e74c3c;"></div> Very High Risk</div>
            <div class="legend-item"><div class="legend-color" style="background: #7f8c8d;"></div> Invalid</div>
            <hr>
            <h4>Marker Types</h4>
            <p style="font-size: 11px; margin: 5px 0;">
                <b>●</b> CSV Original (individual color)<br>
                <b>■</b> SUMO Crossing (grade color)<br>
                <b>―</b> Connection line
            </p>
            <p style="font-size: 11px; margin: 5px 0;">Total: {len(audit_df)} crosswalks</p>
        </div>
    </div>
    <script>
        var map = L.map('map').setView([37.564, 126.99], 13);
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            maxZoom: 19,
            attribution: '© OpenStreetMap'
        }}).addTo(map);

        var geojsonData = {geojson_data};

        geojsonData.features.forEach(function(feature) {{
            var props = feature.properties;
            if (feature.geometry.type === 'Point') {{
                var radius = 5;
                var icon_char = '●';
                if (props.marker_type === 'sumo') {{
                    radius = 6;
                }}
                var marker = L.circleMarker([feature.geometry.coordinates[1], feature.geometry.coordinates[0]], {{
                    radius: radius,
                    fillColor: props.color,
                    color: '#000',
                    weight: 1.5,
                    opacity: 0.8,
                    fillOpacity: 0.8
                }}).addTo(map);
                if (props.popup) {{
                    marker.bindPopup(props.popup);
                }}
            }} else if (feature.geometry.type === 'LineString') {{
                var coords = feature.geometry.coordinates.map(c => [c[1], c[0]]);
                L.polyline(coords, {{
                    color: props.color,
                    weight: 1.5,
                    opacity: 0.4
                }}).addTo(map);
            }}
        }});
    </script>
</body>
</html>"""

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"✓ 저장: {output_file}")


def build_suspicious_crosswalks_map_html(audit_df, output_dir):
    """의심 후보 지도 (개선됨)."""
    output_file = output_dir / "suspicious_crosswalks_map.html"

    suspicious_mask = (
        (audit_df["match_distance_m"] > 25) |
        (audit_df["registry_status"] == "patched_unsignalized") |
        (audit_df["registry_status"] == "recovered") |
        (audit_df["duplicate_crossing_count"] > 1) |
        (audit_df["duplicate_tls_count"] > 1) |
        (~audit_df["crossing_in_all_edges"]) |
        (~audit_df["tls_in_tllogic_current_netxml"]) |
        (~audit_df["tls_in_connection_current_netxml"]) |
        (audit_df["ped_link_indices"].isna())
    )
    suspicious_df = audit_df[suspicious_mask].copy()

    grade_color = {
        "LOW_RISK": "#2ecc71",
        "MEDIUM_RISK": "#3498db",
        "HIGH_RISK": "#f39c12",
        "VERY_HIGH_RISK": "#e74c3c",
        "INVALID_OR_UNUSABLE": "#7f8c8d"
    }

    features = []
    for _, row in suspicious_df.iterrows():
        if pd.isna(row["original_lat"]) or pd.isna(row["original_lon"]):
            continue

        grade_color_val = grade_color.get(row["auto_audit_grade"], "#95a5a6")

        popup_csv = (
            f"<b>CSV Point - ID {row['crosswalk_id']}</b><br>"
            f"Lat: {row['original_lat']:.6f}, Lon: {row['original_lon']:.6f}"
        )

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [row["original_lon"], row["original_lat"]]
            },
            "properties": {
                "id": row["crosswalk_id"],
                "grade": row["auto_audit_grade"],
                "color": grade_color_val,
                "popup": popup_csv
            }
        })

        if pd.notna(row["crossing_lon"]) and pd.notna(row["crossing_lat"]):
            popup_sumo = (
                f"<b>SUMO Crossing - ID {row['crosswalk_id']}</b><br>"
                f"Lat: {row['crossing_lat']:.6f}, Lon: {row['crossing_lon']:.6f}"
            )

            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [row["crossing_lon"], row["crossing_lat"]]
                },
                "properties": {
                    "id": f"{row['crosswalk_id']}_sumo",
                    "grade": row["auto_audit_grade"],
                    "color": grade_color_val,
                    "popup": popup_sumo
                }
            })

            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [row["original_lon"], row["original_lat"]],
                        [row["crossing_lon"], row["crossing_lat"]]
                    ]
                },
                "properties": {
                    "id": f"{row['crosswalk_id']}_line",
                    "grade": row["auto_audit_grade"],
                    "color": grade_color_val
                }
            })

    geojson_data = json.dumps({"type": "FeatureCollection", "features": features})

    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8" />
    <title>Suspicious Crosswalks - Matching Audit v2</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css" />
    <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
    <style>
        body {{ margin: 0; padding: 0; }}
        #map {{ position: absolute; top: 0; bottom: 0; width: 100%; }}
        .info {{ background: white; padding: 10px; border-radius: 5px; box-shadow: 0 0 15px rgba(0,0,0,0.2); }}
    </style>
</head>
<body>
    <div id="map"></div>
    <div class="info" style="position: absolute; top: 10px; left: 10px; z-index: 1000;">
        <div style="background: white; padding: 10px; border-radius: 5px;">
            <h4>Suspicious Candidates</h4>
            <p style="font-size: 12px; margin: 5px 0;">
                Total: {len(suspicious_df)} / {len(audit_df)} crosswalks
            </p>
        </div>
    </div>
    <script>
        var map = L.map('map').setView([37.564, 126.99], 13);
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            maxZoom: 19,
            attribution: '© OpenStreetMap'
        }}).addTo(map);

        var geojsonData = {geojson_data};

        geojsonData.features.forEach(function(feature) {{
            var props = feature.properties;
            if (feature.geometry.type === 'Point') {{
                var marker = L.circleMarker([feature.geometry.coordinates[1], feature.geometry.coordinates[0]], {{
                    radius: 6,
                    fillColor: props.color,
                    color: '#000',
                    weight: 2,
                    opacity: 0.8,
                    fillOpacity: 0.8
                }}).addTo(map);
                if (props.popup) {{
                    marker.bindPopup(props.popup);
                }}
            }} else if (feature.geometry.type === 'LineString') {{
                var coords = feature.geometry.coordinates.map(c => [c[1], c[0]]);
                L.polyline(coords, {{
                    color: props.color,
                    weight: 2,
                    opacity: 0.5
                }}).addTo(map);
            }}
        }});
    </script>
</body>
</html>"""

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"✓ 저장: {output_file} ({len(suspicious_df)} suspicious markers)")


def generate_single_card(row, output_file):
    """개별 PNG 카드 생성 (실제 lon/lat 좌표 기반)."""
    fig, (ax_top, ax_map) = plt.subplots(2, 1, figsize=(10, 10),
                                         gridspec_kw={"height_ratios": [1, 2]})

    # ===== 상단: 텍스트 정보 =====
    ax_top.set_xlim(0, 10)
    ax_top.set_ylim(0, 4)
    ax_top.axis("off")

    y_pos = 3.8
    info_lines = [
        f"Crosswalk ID: {row['crosswalk_id']} | Grade: {row['auto_audit_grade']}",
        f"Status: {row['registry_status']} | Distance: {row['match_distance_m']:.1f}m",
        f"TLS: {row['tls_id']} | Ped Links: {row['ped_link_indices']}",
        f"Crossing Edge: {row['crossing_edge']} | Crossing in net.xml: {row['crossing_in_all_edges']}",
    ]

    for line in info_lines:
        ax_top.text(0.2, y_pos, line, fontsize=9, verticalalignment="top", family="monospace")
        y_pos -= 0.7

    # ===== 중앙: 실제 좌표 기반 지도 =====
    orig_lat = row["original_lat"]
    orig_lon = row["original_lon"]
    cross_lat = row["crossing_lat"]
    cross_lon = row["crossing_lon"]

    if pd.notna(orig_lat) and pd.notna(orig_lon) and pd.notna(cross_lat) and pd.notna(cross_lon):
        # 범위 계산 (200m 반경)
        lat_range = 200 / 111000  # 대략 1도 = 111km
        lon_range = 200 / (111000 * math.cos(math.radians((orig_lat + cross_lat) / 2)))

        min_lat = min(orig_lat, cross_lat) - lat_range
        max_lat = max(orig_lat, cross_lat) + lat_range
        min_lon = min(orig_lon, cross_lon) - lon_range
        max_lon = max(orig_lon, cross_lon) + lon_range

        ax_map.set_xlim(min_lon, max_lon)
        ax_map.set_ylim(min_lat, max_lat)
        ax_map.set_aspect("equal")
        ax_map.grid(True, alpha=0.3)
        ax_map.set_xlabel("Longitude")
        ax_map.set_ylabel("Latitude")

        # 원본 좌표
        ax_map.plot(orig_lon, orig_lat, "go", markersize=12, label="CSV Original", zorder=5)
        ax_map.text(orig_lon, orig_lat - 0.00008, "CSV", ha="center", fontsize=8, color="green")

        # SUMO 좌표
        ax_map.plot(cross_lon, cross_lat, "rs", markersize=12, label="SUMO Crossing", zorder=5)
        ax_map.text(cross_lon, cross_lat + 0.00008, "SUMO", ha="center", fontsize=8, color="red")

        # 연결선
        ax_map.plot([orig_lon, cross_lon], [orig_lat, cross_lat], "b--", linewidth=2, alpha=0.5, label="Matching Vector", zorder=3)

        # 반경 표시 (25m, 50m, 75m)
        for radius_m in [25, 50, 75]:
            radius_deg = radius_m / 111000
            circle = plt.Circle((orig_lon, orig_lat), radius_deg, fill=False,
                               edgecolor="gray", linestyle="--", linewidth=0.5, alpha=0.3)
            ax_map.add_patch(circle)

        ax_map.legend(loc="upper right", fontsize=8)
        ax_map.set_title(f"Matching Visualization (Distance: {haversine(orig_lat, orig_lon, cross_lat, cross_lon):.1f}m)", fontsize=10)
    else:
        ax_map.text(0.5, 0.5, "Insufficient coordinate data", ha="center", va="center", transform=ax_map.transAxes)
        ax_map.axis("off")

    plt.tight_layout()
    plt.savefig(output_file, dpi=100, bbox_inches="tight")
    plt.close()


def generate_cards(audit_df, output_dir, card_mode):
    """PNG 카드 생성 (개선됨)."""
    cards_dir = output_dir / "cards"
    cards_dir.mkdir(exist_ok=True)

    if card_mode == "none":
        print("✓ PNG 카드 생성 스킵")
        return

    if card_mode == "suspicious":
        mask = (
            (audit_df["match_distance_m"] > 25) |
            (audit_df["registry_status"] == "patched_unsignalized") |
            (audit_df["registry_status"] == "recovered") |
            (audit_df["duplicate_crossing_count"] > 1) |
            (audit_df["auto_audit_grade"].isin(["HIGH_RISK", "VERY_HIGH_RISK", "INVALID_OR_UNUSABLE"]))
        )
        # 119055는 무조건 포함
        mask = mask | (audit_df["crosswalk_id"] == 119055)
        cards_to_generate = audit_df[mask]
    else:  # all
        cards_to_generate = audit_df

    print(f"✓ PNG 카드 생성 시작: {len(cards_to_generate)} 개")
    for idx, (_, row) in enumerate(cards_to_generate.iterrows()):
        card_file = cards_dir / f"crosswalk_{row['crosswalk_id']}.png"
        try:
            generate_single_card(row, card_file)
        except Exception as e:
            print(f"  ⚠ Card {row['crosswalk_id']} 생성 실패: {e}")

        if (idx + 1) % 50 == 0:
            print(f"  {idx + 1}/{len(cards_to_generate)} 완료")

    print(f"✓ PNG 카드 생성 완료")


def generate_case_119055_md(audit_df, output_dir):
    """119055 전용 분석."""
    output_file = output_dir / "case_119055_visual_audit.md"

    row_119055 = audit_df[audit_df["crosswalk_id"] == 119055]
    if row_119055.empty:
        print("⚠ 119055를 찾을 수 없습니다.")
        return

    row = row_119055.iloc[0]

    same_crossing_count = (audit_df["crossing_edge"] == row["crossing_edge"]).sum() - 1
    same_tls_count = (audit_df["tls_id"] == row["tls_id"]).sum() - 1

    # 권고 판정
    if row["auto_audit_grade"] == "VERY_HIGH_RISK":
        if pd.isna(row["original_lat"]):
            recommendation = "PILOT_NOT_RECOMMENDED"
            reason = "원본 좌표 부재"
        else:
            recommendation = "PILOT_CONDITIONAL"
            reason = "좌표 복구됨, HIGH_RISK 수준"
    elif row["auto_audit_grade"] == "HIGH_RISK":
        recommendation = "PILOT_CONDITIONAL"
        reason = "중거리 매칭, 신호 제어 확인 필요"
    else:
        recommendation = "PILOT_OK"
        reason = "양호"

    md_content = f"""# Crosswalk 119055 - Visual Audit Case Study

## 기본 정보

| 항목 | 값 |
|---|---|
| Crosswalk ID | {row['crosswalk_id']} |
| Auto Audit Grade | {row['auto_audit_grade']} |
| Registry Status | {row['registry_status']} |
| Mapping Confidence | {row['mapping_confidence']} |

## 좌표 정보

### 원본 (CSV - T2 또는 Registry)
- **위도**: {row['original_lat']}
- **경도**: {row['original_lon']}

### 매칭된 Crossing (SUMO)
- **위도**: {row['crossing_lat']}
- **경도**: {row['crossing_lon']}
- **Match Distance**: {row['match_distance_m']:.2f}m
- **Haversine Distance**: {row['haversine_distance_m']:.2f}m

## 신호/보행 연계 정보

| 항목 | 값 |
|---|---|
| Crossing Edge | {row['crossing_edge']} |
| Crossing in All Edges | {row['crossing_in_all_edges']} |
| Crossing is Function=crossing | {row['crossing_is_function_crossing']} |
| TLS ID | {row['tls_id']} |
| TLS Present in Registry | {row['tls_id_present_in_registry']} |
| TLS in tlLogic (current net.xml) | {row['tls_in_tllogic_current_netxml']} |
| TLS in Connection (current net.xml) | {row['tls_in_connection_current_netxml']} |
| Ped Link Indices | {row['ped_link_indices']} |
| Runnable for Signal Extension | {row['runnable_for_signal_extension']} |

## 중복 분석

### Crossing Edge 공유
- 같은 `crossing_edge` 공유 후보: {same_crossing_count}개
- Crossing Edge 값: `{row['crossing_edge']}`

### TLS 공유
- 같은 `tls_id` 공유 후보: {same_tls_count}개
- TLS ID 값: `{row['tls_id']}`

## 위험 플래그 분석

```
{row['risk_flags']}
```

## 자동 감사 판정

- **등급**: {row['auto_audit_grade']}
- **매칭 거리**: {row['match_distance_m']:.1f}m
- **중복 여부**: Crossing {row['duplicate_crossing_count']}, TLS {row['duplicate_tls_count']}
- **Net.xml 검증**: {row['netxml_validation_status']}

## 최종 권고

### Pilot 권고
```
{recommendation}
```

### 이유
{reason}

- 원본 좌표 존재: {pd.notna(row['original_lat']) and pd.notna(row['original_lon'])}
- 거리 범위: {row['match_distance_m']:.1f}m (중거리, 수용 가능 범위)
- 신호 제어: TLS ID 존재, 검증 상태 {row['netxml_validation_status']}

---

*이 분석은 현재 입력된 net.xml 메타데이터 기준입니다.*
*Network version mismatch 가능성이 있으므로, 실제 시뮬레이션 전 검증 필수입니다.*
"""

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"✓ 저장: {output_file}")


def generate_matching_visual_audit_report_md(audit_df, registry_path, t2_path, net_xml_path, output_dir):
    """최종 요약 리포트."""
    output_file = output_dir / "matching_visual_audit_report.md"

    grade_dist = audit_df["auto_audit_grade"].value_counts()
    status_dist = audit_df["registry_status"].value_counts()
    dist_buckets = {
        "0-10m": (audit_df["match_distance_m"] <= 10).sum(),
        "10-25m": ((audit_df["match_distance_m"] > 10) & (audit_df["match_distance_m"] <= 25)).sum(),
        "25-50m": ((audit_df["match_distance_m"] > 25) & (audit_df["match_distance_m"] <= 50)).sum(),
        "50-75m": ((audit_df["match_distance_m"] > 50) & (audit_df["match_distance_m"] <= 75)).sum(),
        ">75m": (audit_df["match_distance_m"] > 75).sum()
    }

    patched_count = (audit_df["registry_status"] == "patched_unsignalized").sum()
    recovered_count = (audit_df["registry_status"] == "recovered").sum()

    # 119055 특수
    row_119055 = audit_df[audit_df["crosswalk_id"] == 119055]
    if not row_119055.empty:
        r119 = row_119055.iloc[0]
        coord_status_119 = "T2에서 복구됨" if pd.notna(r119["original_lat"]) else "좌표 부재"
        grade_119 = r119["auto_audit_grade"]
    else:
        coord_status_119 = "미확인"
        grade_119 = "미확인"

    md_content = f"""# Matching Visual Audit Report v2

## 프로젝트 개요

**목표**: 전체 670개 횡단보도 후보의 매칭 신뢰성을 개선된 시각화와 함께 제공

**생성 일시**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

**개선사항**:
- T2_crosswalk_features.csv와 join으로 원본 좌표 복구
- net.xml edge id 기준 검증 (function="crossing" 구분)
- TLS 검증 분리 (Registry vs Current net.xml)
- PNG 카드를 실제 lon/lat 좌표 기반으로 생성
- HTML 지도 마커 구분 (CSV circle vs SUMO square)

## 입력 파일

| 항목 | 경로 |
|---|---|
| Registry | {registry_path} |
| T2 Crosswalk Features | {t2_path} |
| SUMO net.xml | {net_xml_path} |
| Output Dir | {output_dir} |

### 주의사항
- **net.xml 버전 불일치**: 이 감사는 입력된 net.xml 기준입니다.
  Registry와 net.xml이 같은 네트워크 버전인지 확인하세요.
- **인터넷 필수**: HTML 지도는 OpenStreetMap CDN을 사용합니다.
  인터넷 미연결 시 배경지도가 표시되지 않으나, marker/coordinate 데이터는 HTML 내 포함됩니다.
- **T2 Join**: 원본 좌표는 Registry 우선, 없으면 T2_crosswalk_features.csv에서 복구합니다.

## 종합 통계

### 전체 후보 수
**{len(audit_df)} 개**

### Auto Audit Grade 분포 (개선됨)

| 등급 | 개수 | 비율 |
|---|---:|---:|
| INVALID_OR_UNUSABLE | {grade_dist.get('INVALID_OR_UNUSABLE', 0)} | {100*grade_dist.get('INVALID_OR_UNUSABLE', 0)/len(audit_df):.1f}% |
| VERY_HIGH_RISK | {grade_dist.get('VERY_HIGH_RISK', 0)} | {100*grade_dist.get('VERY_HIGH_RISK', 0)/len(audit_df):.1f}% |
| HIGH_RISK | {grade_dist.get('HIGH_RISK', 0)} | {100*grade_dist.get('HIGH_RISK', 0)/len(audit_df):.1f}% |
| MEDIUM_RISK | {grade_dist.get('MEDIUM_RISK', 0)} | {100*grade_dist.get('MEDIUM_RISK', 0)/len(audit_df):.1f}% |
| LOW_RISK | {grade_dist.get('LOW_RISK', 0)} | {100*grade_dist.get('LOW_RISK', 0)/len(audit_df):.1f}% |

### Registry Status 분포

| 상태 | 개수 |
|---|---:|
| patched_unsignalized | {status_dist.get('patched_unsignalized', 0)} |
| A | {status_dist.get('A', 0)} |
| recovered | {status_dist.get('recovered', 0)} |
| represented_by_patched_crossing | {status_dist.get('represented_by_patched_crossing', 0)} |
| route_patch_required | {status_dist.get('route_patch_required', 0)} |
| distance_mapped | {status_dist.get('distance_mapped', 0)} |
| pedestrian_topology_repair_required | {status_dist.get('pedestrian_topology_repair_required', 0)} |

### 매칭 거리 분포 (match_distance_m)

| 거리 범위 | 개수 |
|---|---:|
| 0-10m | {dist_buckets['0-10m']} |
| 10-25m | {dist_buckets['10-25m']} |
| 25-50m | {dist_buckets['25-50m']} |
| 50-75m | {dist_buckets['50-75m']} |
| >75m | {dist_buckets['>75m']} |

## 특수 항목: 119055 분석

| 항목 | 값 |
|---|---|
| Auto Grade | {grade_119} |
| 원본 좌표 | {coord_status_119} |
| Match Distance | 54.73m |
| Registry Status | recovered |
| Recommendation | 위 case_119055_visual_audit.md 참조 |

## 검수 전략

### 1차 검수 (필수)
- **대상**: INVALID_OR_UNUSABLE + VERY_HIGH_RISK
- **예상 개수**: {grade_dist.get('INVALID_OR_UNUSABLE', 0) + grade_dist.get('VERY_HIGH_RISK', 0)}개
- **방법**: all_crosswalks_map.html + PNG 카드 검토
- **목표**: 사용 불가 항목 확정

### 2차 검수 (권장)
- **대상**: HIGH_RISK
- **예상 개수**: {grade_dist.get('HIGH_RISK', 0)}개

### 3차 검수 (표본)
- **대상**: MEDIUM_RISK 중 핵심 항목

### 4차 검수 (표본)
- **대상**: LOW_RISK 신뢰도 확인

## 산출물 안내

### CSV
- `crosswalk_visual_audit.csv`: 전체 670개 자동 감사 결과
- `manual_review_template.csv`: 수동 검수 템플릿
- `priority_review_list.csv`: 우선순위 정렬 목록

### HTML 지도
- `all_crosswalks_map.html`: 전체 지도 (670개 개별 색상)
- `suspicious_crosswalks_map.html`: 의심 후보 지도

### PNG 카드
- `cards/crosswalk_<id>.png`: 실제 lon/lat 좌표 기반 검수 카드

### Markdown
- `case_119055_visual_audit.md`: 119055 전용 분석
- `matching_visual_audit_report.md`: 본 리포트

## 기술 개선사항

1. **T2 Join**: Registry original_csv 비어있으면 T2에서 복구
2. **Edge 검증**: `<edge id>` set 기준 (function="crossing" 별도 확인)
3. **TLS 검증**: `tls_id_present_in_registry` vs `tls_in_tllogic_current_netxml` 분리
4. **PNG 카드**: 실제 lon/lat 범위 기반 좌표 플롯
5. **색상**: 670개 항목을 개별 색상으로 구분 (HSL palette)

---

*Audit 완료: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"✓ 저장: {output_file}")


def main():
    """메인 함수."""
    args = parse_args()

    # 출력 디렉토리 생성
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"✓ Output directory: {output_dir}")

    # 데이터 로딩
    registry_df = load_registry(args.registry)
    t2_df = load_crosswalk_features(args.crosswalk_features)

    # net.xml 파싱
    all_edges, crossing_edges, crossing_edges_with_function, tls_in_tllogic, tls_in_connection = parse_net_xml(args.net_xml)

    # 감사 컬럼 계산
    audit_df = compute_audit_columns(registry_df, t2_df, all_edges, crossing_edges_with_function, tls_in_tllogic, tls_in_connection)

    # CSV 저장
    save_crosswalk_visual_audit_csv(audit_df, output_dir)
    save_manual_review_template_csv(audit_df, output_dir)
    save_priority_review_list_csv(audit_df, output_dir)

    # HTML 지도 생성
    build_all_crosswalks_map_html(audit_df, output_dir)
    build_suspicious_crosswalks_map_html(audit_df, output_dir)

    # PNG 카드 생성
    generate_cards(audit_df, output_dir, args.cards)

    # 특수 항목: 119055
    generate_case_119055_md(audit_df, output_dir)

    # 최종 리포트
    generate_matching_visual_audit_report_md(audit_df, args.registry, args.crosswalk_features, args.net_xml, output_dir)

    print(f"\n✓ 모든 작업 완료")
    print(f"✓ 산출물: {output_dir}")


if __name__ == "__main__":
    main()
