"""
map_candidates_to_generated_signal_7.py

34개 후보 crosswalk의 실세계 좌표를 generated_signal_7.net.xml의
기존 crossing/TLS 구조에 점수 기반으로 매핑하고, 사람이 검토할 수 있는
HTML 리뷰 파일과 CSV를 생성한다.

사용법:
    cd /Users/junlee/Desktop/2026-1/js
    python3 analysis/map_candidates_to_generated_signal_7.py

출력: result/osm_audit/
  1. generated_signal_7_all_candidate_mapping_audit.csv    (후보별 best 매핑)
  2. generated_signal_7_candidate_mapping_candidates_long.csv  (top5 long format)
  3. generated_signal_7_candidate_review_links.csv          (지도 링크)
  4. generated_signal_7_candidate_review.html               (사람 검토용)
  5. generated_signal_7_all_candidate_mapping.geojson       (시각화용)
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd
import sumolib

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
JS_ROOT = Path(__file__).resolve().parent.parent
NET_PATH = JS_ROOT / "result" / "active" / "nets" / "generated_signal_7.net.xml"
CANDIDATE_META = JS_ROOT / "final" / "manifests" / "candidate_metadata.csv"
STEPWISE_CSV = JS_ROOT / "crosswalk_stepwise_result_50m.csv"
OUTPUT_DIR = JS_ROOT / "result" / "osm_audit"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── 매핑 상수 ─────────────────────────────────────────────────────────────────
TOP_K = 10         # 후보당 상위 몇 개 crossing 저장 (greedy 재배정 여유 확보용)
MAX_SEARCH_M = 400  # 이 반경 안의 crossing만 고려 (m)

# 거리 기준 (TLS 있는 경우 / 없는 경우 별도 점수)
# (dist_threshold, level, score_with_tls, score_no_tls)
LEVEL_THRESHOLDS = [
    (10,  "exact",    100,  60),
    (30,  "good",      80,  45),
    (75,  "approx",   55,  25),
    (150, "distant",  30,  10),
    (MAX_SEARCH_M, "far", 10,  5),
]

# ── net.xml 로드 ──────────────────────────────────────────────────────────────
print("net.xml 로드 중... (21207 crossing 파싱, 약 20~40초 소요)")
t0 = time.time()
net = sumolib.net.readNet(str(NET_PATH), withInternal=True)
print(f"  완료 ({time.time()-t0:.1f}s)")

# ── TLS junction 집합 ─────────────────────────────────────────────────────────
print("TLS junction 수집 중...")
tl_node_ids: set[str] = set()
tl_positions: list[tuple[float, float, str]] = []   # (x, y, tls_id)

for node in net.getNodes():
    if node.getType() in ("traffic_light", "traffic_light_right_on_red",
                           "traffic_light_unregulated"):
        tl_node_ids.add(node.getID())
        x, y = node.getCoord()
        tl_positions.append((x, y, node.getID()))

print(f"  TLS junction 수: {len(tl_node_ids)}")

# TLS KDTree
tl_xy = np.array([(p[0], p[1]) for p in tl_positions])

# ── crossing edge 추출 ────────────────────────────────────────────────────────
print("crossing edge 중심 좌표 추출 중...")
t0 = time.time()

crossing_data: list[dict] = []

for edge in net.getEdges(withInternal=True):
    if edge.getFunction() != "crossing":
        continue
    lanes = edge.getLanes()
    if not lanes:
        continue
    shape = lanes[0].getShape()
    if len(shape) < 2:
        cx, cy = shape[0]
    else:
        cx = sum(p[0] for p in shape) / len(shape)
        cy = sum(p[1] for p in shape) / len(shape)

    # junction ID 추출: :JUNCTION_ID_cN 패턴
    eid = edge.getID()
    m = re.match(r"^:(.+)_c\d+$", eid)
    junction_id = m.group(1) if m else ""

    has_tls = junction_id in tl_node_ids

    crossing_data.append({
        "crossing_edge_id": eid,
        "junction_id": junction_id,
        "has_tls": has_tls,
        "cx": cx,
        "cy": cy,
    })

print(f"  crossing 수: {len(crossing_data)}  ({time.time()-t0:.1f}s)")

crossing_xy = np.array([[d["cx"], d["cy"]] for d in crossing_data])

# ── scipy KDTree로 빠른 nearest-neighbor ─────────────────────────────────────
try:
    from scipy.spatial import KDTree
    tree = KDTree(crossing_xy)
    USE_KDTREE = True
    tl_tree = KDTree(tl_xy) if len(tl_xy) > 0 else None
    print("  KDTree 구축 완료")
except ImportError:
    USE_KDTREE = False
    tl_tree = None
    print("  scipy 없음 – 선형 탐색 사용 (느릴 수 있음)")

# ── 후보 좌표 로드 ────────────────────────────────────────────────────────────
print("\n후보 좌표 로드 중...")
try:
    step_df = pd.read_csv(STEPWISE_CSV, encoding="cp949")
except UnicodeDecodeError:
    step_df = pd.read_csv(STEPWISE_CSV, encoding="utf-8")
step_df["crosswalk_id"] = step_df["crosswalk_id"].astype(str)

meta_df = pd.read_csv(CANDIDATE_META)
meta_df["crosswalk_id"] = meta_df["crosswalk_id"].astype(str)

# 34개 후보에 대해 좌표 병합 (meta의 dong_name이 있으면 우선, 없으면 stepwise 값 사용)
step_sub = step_df[["crosswalk_id", "lat", "lon", "admin_dong",
                     "accident_count", "elderly_ratio",
                     "dong_name"]].drop_duplicates("crosswalk_id").rename(
    columns={"dong_name": "dong_name_step"}
)
candidates = meta_df.merge(step_sub, on="crosswalk_id", how="left")
# dong_name 우선순위: meta > stepwise
if "dong_name" not in candidates.columns:
    candidates["dong_name"] = candidates.get("dong_name_step", "")
else:
    candidates["dong_name"] = candidates["dong_name"].fillna(candidates.get("dong_name_step", ""))
print(f"  후보 수: {len(candidates)}  (좌표 있는 것: {candidates['lat'].notna().sum()})")

# ── 매핑 함수 ─────────────────────────────────────────────────────────────────
def dist_m(x1, y1, x2, y2) -> float:
    return math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)

def get_level_score(d: float, has_tls: bool) -> tuple[str, int]:
    for threshold, level, score_tls, score_no_tls in LEVEL_THRESHOLDS:
        if d <= threshold:
            return level, (score_tls if has_tls else score_no_tls)
    return "out_of_range", 0

def get_top_k_crossings(cand_x: float, cand_y: float) -> list[dict]:
    if USE_KDTREE:
        # MAX_SEARCH_M 반경 안에서 top-K 검색
        idxs = tree.query_ball_point([cand_x, cand_y], MAX_SEARCH_M)
        if not idxs:
            # 반경 확장
            dists, idxs = tree.query([cand_x, cand_y], k=min(TOP_K * 3, len(crossing_data)))
            idxs = list(idxs)
    else:
        dists_all = np.sqrt(((crossing_xy - np.array([cand_x, cand_y])) ** 2).sum(axis=1))
        idxs = np.where(dists_all <= MAX_SEARCH_M)[0].tolist()
        if not idxs:
            idxs = np.argsort(dists_all)[:TOP_K * 3].tolist()

    results = []
    for i in idxs:
        d = dist_m(cand_x, cand_y, crossing_data[i]["cx"], crossing_data[i]["cy"])
        level, score = get_level_score(d, crossing_data[i]["has_tls"])
        results.append({
            **crossing_data[i],
            "distance_m": round(d, 2),
            "mapping_level": level,
            "mapping_score": score,
        })

    results.sort(key=lambda r: r["distance_m"])
    return results[:TOP_K]

def get_nearest_tls(cand_x: float, cand_y: float) -> tuple[str, float]:
    if tl_tree is None:
        return "", float("inf")
    d, i = tl_tree.query([cand_x, cand_y])
    return tl_positions[int(i)][2], float(d)

# ── 1단계: 모든 후보의 top-K 수집 ─────────────────────────────────────────────
print("\n매핑 실행 중...")

# 후보별 top-K 리스트와 기본 메타 수집
cand_topk: list[dict] = []   # {cw_id, cand_x, cand_y, lat, lon, meta, topk, tls_id, tls_dist}

for _, cand in candidates.iterrows():
    cw_id = str(cand["crosswalk_id"])
    lat = cand.get("lat")
    lon = cand.get("lon")

    if pd.isna(lat) or pd.isna(lon):
        cand_topk.append({"cw_id": cw_id, "cand": cand, "topk": [], "no_coord": True})
        continue

    cand_x, cand_y = net.convertLonLat2XY(float(lon), float(lat))
    top_k = get_top_k_crossings(cand_x, cand_y)
    tls_id, tls_dist = get_nearest_tls(cand_x, cand_y)
    cand_topk.append({
        "cw_id": cw_id, "cand": cand,
        "lat": float(lat), "lon": float(lon),
        "cand_x": cand_x, "cand_y": cand_y,
        "topk": top_k,
        "tls_id": tls_id, "tls_dist": tls_dist,
        "no_coord": False,
    })

# ── 2단계: greedy 고유 배정 (거리 오름차순) ────────────────────────────────────
# 모든 (후보, crossing, 거리) 조합을 거리 오름차순으로 정렬
# → 가장 가까운 쌍부터 배정, 이미 배정된 crossing은 스킵
print("  greedy 고유 배정 중...")

all_pairs: list[tuple[float, str, int]] = []  # (distance, cw_id, topk_index)
for item in cand_topk:
    if item["no_coord"]:
        continue
    for ki, hit in enumerate(item["topk"]):
        all_pairs.append((hit["distance_m"], item["cw_id"], ki))

all_pairs.sort(key=lambda x: x[0])

assigned_crossing: dict[str, str] = {}   # cw_id → crossing_edge_id
used_crossings: set[str] = set()

for dist_v, cw_id, ki in all_pairs:
    if cw_id in assigned_crossing:
        continue  # 이미 배정됨
    crossing_id = None
    # ki부터 순서대로 아직 사용되지 않은 crossing 찾기
    item = next(x for x in cand_topk if x["cw_id"] == cw_id)
    for hit in item["topk"][ki:]:
        if hit["crossing_edge_id"] not in used_crossings:
            crossing_id = hit["crossing_edge_id"]
            assigned_crossing[cw_id] = crossing_id
            used_crossings.add(crossing_id)
            break

# 아직 배정 못 받은 후보: top-K 전체 스캔 (used_crossings 무시하고 best 선택, 중복 허용)
for item in cand_topk:
    if item["no_coord"]:
        continue
    if item["cw_id"] not in assigned_crossing:
        if item["topk"]:
            hit = item["topk"][0]
            assigned_crossing[item["cw_id"]] = hit["crossing_edge_id"]
            print(f"  ⚠ {item['cw_id']}: top-K 내 고유 crossing 없음 → best 재사용 허용")

dup_after = len(assigned_crossing) - len(set(assigned_crossing.values()))
print(f"  배정 완료: {len(assigned_crossing)}개  남은 중복: {dup_after}개")

# ── 3단계: 결과 조합 ──────────────────────────────────────────────────────────
audit_rows = []       # best 매핑 1행/후보
long_rows = []        # top-5 long format
link_rows = []        # 지도 링크

for item in cand_topk:
    cw_id = item["cw_id"]
    cand = item["cand"]

    if item["no_coord"]:
        audit_rows.append({
            "crosswalk_id": cw_id,
            "candidate_lat": None, "candidate_lon": None,
            "mapped_crossing_edge_id": "", "mapped_junction_id": "",
            "mapped_has_tls": False, "mapped_lat": None, "mapped_lon": None,
            "crossing_distance_m": None, "tls_id_nearest": "",
            "tls_distance_m": None, "mapping_score": 0,
            "mapping_level": "no_coord", "mapping_reason": "후보 좌표 없음",
        })
        continue

    lat, lon = item["lat"], item["lon"]
    tls_id, tls_dist = item["tls_id"], item["tls_dist"]
    assigned_edge = assigned_crossing.get(cw_id)

    # assigned crossing 찾기
    best = next((h for h in item["topk"] if h["crossing_edge_id"] == assigned_edge), None)
    if best is None and item["topk"]:
        best = item["topk"][0]   # fallback

    # 원래 1순위와 배정 결과가 다른지 표시
    original_best = item["topk"][0] if item["topk"] else None
    was_reassigned = (
        original_best is not None and
        best is not None and
        best["crossing_edge_id"] != original_best["crossing_edge_id"]
    )

    if best:
        mapped_lon, mapped_lat = net.convertXY2LonLat(best["cx"], best["cy"])
        level = best["mapping_level"]
        reason_parts = [f"{'재배정 ' if was_reassigned else ''}nearest crossing {best['distance_m']:.1f}m"]
        if was_reassigned:
            reason_parts.append(f"원래 1순위({original_best['crossing_edge_id']}) 중복 → 재배정")
        if best["has_tls"]:
            reason_parts.append("TLS junction 있음")
        if level in ("exact", "good"):
            reason_parts.append("매핑 신뢰도 높음")
        elif level == "approx":
            reason_parts.append("중간 거리 – 검토 권장")
        else:
            reason_parts.append("거리 큼 – 수동 확인 필요")
    else:
        mapped_lon = mapped_lat = None
        level = "out_of_range"
        reason_parts = [f"반경 {MAX_SEARCH_M}m 내 crossing 없음"]

    audit_rows.append({
        "crosswalk_id": cw_id,
        "dong_name": cand.get("dong_name", ""),
        "candidate_lat": round(lat, 6),
        "candidate_lon": round(lon, 6),
        "mapped_crossing_edge_id": best["crossing_edge_id"] if best else "",
        "mapped_junction_id": best["junction_id"] if best else "",
        "mapped_has_tls": best["has_tls"] if best else False,
        "mapped_lat": round(mapped_lat, 6) if mapped_lat else None,
        "mapped_lon": round(mapped_lon, 6) if mapped_lon else None,
        "crossing_distance_m": best["distance_m"] if best else None,
        "tls_id_nearest": tls_id,
        "tls_distance_m": round(tls_dist, 2),
        "mapping_score": best["mapping_score"] if best else 0,
        "mapping_level": level,
        "mapping_reason": " / ".join(reason_parts),
        "was_reassigned": was_reassigned,
        "original_crossing_edge_id": original_best["crossing_edge_id"] if (was_reassigned and original_best) else "",
        "net_group": cand.get("net_group", ""),
        "risk_rank": cand.get("risk_rank", ""),
        "risk_score": cand.get("risk_score", ""),
        "accident_count": cand.get("accident_count", ""),
    })

    # long format (top-5)
    for rank_i, hit in enumerate(item["topk"], 1):
        h_lon, h_lat = net.convertXY2LonLat(hit["cx"], hit["cy"])
        long_rows.append({
            "crosswalk_id": cw_id,
            "candidate_lat": round(lat, 6),
            "candidate_lon": round(lon, 6),
            "rank": rank_i,
            "is_assigned": hit["crossing_edge_id"] == assigned_crossing.get(cw_id),
            "crossing_edge_id": hit["crossing_edge_id"],
            "junction_id": hit["junction_id"],
            "has_tls": hit["has_tls"],
            "mapped_lat": round(h_lat, 6),
            "mapped_lon": round(h_lon, 6),
            "distance_m": hit["distance_m"],
            "mapping_level": hit["mapping_level"],
            "mapping_score": hit["mapping_score"],
        })

    # 지도 링크
    gmaps_cand = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
    gmaps_mapped = (
        f"https://www.google.com/maps/search/?api=1&query={mapped_lat},{mapped_lon}"
        if mapped_lat else ""
    )
    link_rows.append({
        "crosswalk_id": cw_id,
        "dong_name": cand.get("dong_name", ""),
        "candidate_lat": round(lat, 6),
        "candidate_lon": round(lon, 6),
        "was_reassigned": was_reassigned,
        "mapped_lat": round(mapped_lat, 6) if mapped_lat else None,
        "mapped_lon": round(mapped_lon, 6) if mapped_lon else None,
        "crossing_distance_m": best["distance_m"] if best else None,
        "mapping_level": level,
        "mapping_score": best["mapping_score"] if best else 0,
        "google_maps_candidate_url": gmaps_cand,
        "google_maps_mapped_url": gmaps_mapped,
        "naver_search_text": f"{lat},{lon}",
        "kakao_search_text": f"{lat},{lon}",
        "manual_status": "",
        "manual_note": "",
    })

print(f"  완료: {len(audit_rows)}개 후보 매핑")

# ── CSV 저장 ──────────────────────────────────────────────────────────────────
audit_df = pd.DataFrame(audit_rows)
long_df = pd.DataFrame(long_rows)
link_df = pd.DataFrame(link_rows)

AUDIT_CSV = OUTPUT_DIR / "generated_signal_7_all_candidate_mapping_audit.csv"
LONG_CSV = OUTPUT_DIR / "generated_signal_7_candidate_mapping_candidates_long.csv"
LINK_CSV = OUTPUT_DIR / "generated_signal_7_candidate_review_links.csv"

audit_df.to_csv(AUDIT_CSV, index=False, encoding="utf-8-sig")
long_df.to_csv(LONG_CSV, index=False, encoding="utf-8-sig")
link_df.to_csv(LINK_CSV, index=False, encoding="utf-8-sig")

print(f"\n  ✓ {AUDIT_CSV.name}  ({len(audit_df)} 행)")
print(f"  ✓ {LONG_CSV.name}  ({len(long_df)} 행)")
print(f"  ✓ {LINK_CSV.name}  ({len(link_df)} 행)")

# ── GeoJSON 저장 ──────────────────────────────────────────────────────────────
features = []
for row in audit_rows:
    lat_v = row.get("candidate_lat")
    lon_v = row.get("candidate_lon")
    if lat_v is None or lon_v is None:
        continue

    features.append({
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [float(lon_v), float(lat_v)]},
        "properties": {
            "feature_type": "candidate",
            "crosswalk_id": row["crosswalk_id"],
            "dong_name": row.get("dong_name", ""),
            "mapping_level": row["mapping_level"],
            "mapping_score": row["mapping_score"],
            "crossing_distance_m": row.get("crossing_distance_m"),
        },
    })

    # mapped crossing point
    m_lat = row.get("mapped_lat")
    m_lon = row.get("mapped_lon")
    if m_lat and m_lon:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(m_lon), float(m_lat)]},
            "properties": {
                "feature_type": "mapped_crossing",
                "crosswalk_id": row["crosswalk_id"],
                "crossing_edge_id": row.get("mapped_crossing_edge_id", ""),
                "has_tls": row.get("mapped_has_tls", False),
                "distance_from_candidate_m": row.get("crossing_distance_m"),
            },
        })

    # 연결선
    if m_lat and m_lon:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [float(lon_v), float(lat_v)],
                    [float(m_lon), float(m_lat)],
                ],
            },
            "properties": {
                "feature_type": "mapping_line",
                "crosswalk_id": row["crosswalk_id"],
                "distance_m": row.get("crossing_distance_m"),
                "mapping_level": row["mapping_level"],
            },
        })

geojson = {
    "type": "FeatureCollection",
    "name": "generated_signal_7 후보 매핑 감사",
    "features": features,
}
GEOJSON_PATH = OUTPUT_DIR / "generated_signal_7_all_candidate_mapping.geojson"
with open(GEOJSON_PATH, "w", encoding="utf-8") as f:
    json.dump(geojson, f, ensure_ascii=False, indent=2)
print(f"  ✓ {GEOJSON_PATH.name}  ({len(features)} features)")

# ── HTML review 생성 ──────────────────────────────────────────────────────────
print("\nHTML review 파일 생성 중...")

LEVEL_COLOR = {
    "exact": "#22c55e",
    "good": "#84cc16",
    "approx": "#f59e0b",
    "distant": "#ef4444",
    "far": "#dc2626",
    "out_of_range": "#6b7280",
    "no_coord": "#6b7280",
}

# long_df를 crosswalk_id별 dict로 변환
long_by_id: dict[str, list[dict]] = {}
for row in long_rows:
    long_by_id.setdefault(row["crosswalk_id"], []).append(row)

def fmt(v, decimals=3):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    if isinstance(v, float):
        return f"{v:.{decimals}f}"
    return str(v)

cards_html = []
for row in audit_rows:
    cw_id = row["crosswalk_id"]
    level = row["mapping_level"]
    color = LEVEL_COLOR.get(level, "#6b7280")
    score = row.get("mapping_score", 0)
    dist = row.get("crossing_distance_m")
    tls_dist = row.get("tls_distance_m")

    gmaps_cand = f"https://www.google.com/maps/search/?api=1&query={row.get('candidate_lat')},{row.get('candidate_lon')}"
    gmaps_mapped = ""
    if row.get("mapped_lat") and row.get("mapped_lon"):
        gmaps_mapped = f"https://www.google.com/maps/search/?api=1&query={row.get('mapped_lat')},{row.get('mapped_lon')}"

    # top-5 table
    top5_rows_html = ""
    for h in long_by_id.get(cw_id, []):
        h_color = LEVEL_COLOR.get(h["mapping_level"], "#6b7280")
        tls_badge = '<span class="tls-badge">TLS</span>' if h["has_tls"] else ""
        top5_rows_html += f"""
        <tr>
          <td>{h['rank']}</td>
          <td class="mono">{h['crossing_edge_id']}</td>
          <td>{tls_badge}</td>
          <td>{fmt(h['distance_m'], 1)}m</td>
          <td><span class="badge" style="background:{h_color}">{h['mapping_level']}</span></td>
          <td>{h['mapping_score']}</td>
          <td>{fmt(h['mapped_lat'], 6)}, {fmt(h['mapped_lon'], 6)}</td>
          <td><a href="https://www.google.com/maps/search/?api=1&query={h['mapped_lat']},{h['mapped_lon']}" target="_blank">지도</a></td>
        </tr>"""

    reassigned_badge = '<span class="badge" style="background:#f97316">재배정</span>' if row.get("was_reassigned") else ""
    orig_edge_note = f'<p style="color:#f97316;font-size:0.8rem">원래 1순위: <code>{row.get("original_crossing_edge_id","")}</code> (중복 충돌)</p>' if row.get("was_reassigned") else ""

    card = f"""
  <div class="card" id="card-{cw_id}">
    <div class="card-header" style="border-left: 5px solid {color}">
      <div class="card-title">
        <span class="cw-id">{cw_id}</span>
        <span class="dong">{row.get('dong_name', '')}</span>
        <span class="badge" style="background:{color}">{level}</span>
        {reassigned_badge}
        <span class="score">점수 {score}</span>
      </div>
      <div class="card-meta">
        위험순위 {fmt(row.get('risk_rank', ''), 0)} &nbsp;|&nbsp;
        사고수 {fmt(row.get('accident_count', ''), 0)} &nbsp;|&nbsp;
        net_group: {row.get('net_group', '')}
      </div>
    </div>

    <div class="card-body">
      <div class="col">
        <h4>후보 원좌표</h4>
        <p>lat: <b>{fmt(row.get('candidate_lat'), 6)}</b></p>
        <p>lon: <b>{fmt(row.get('candidate_lon'), 6)}</b></p>
        <p>네이버 검색: <code>{row.get('candidate_lat')},{row.get('candidate_lon')}</code></p>
        <p>카카오 검색: <code>{row.get('candidate_lat')},{row.get('candidate_lon')}</code></p>
        <a class="map-btn" href="{gmaps_cand}" target="_blank">Google Maps 원좌표</a>
      </div>

      <div class="col">
        <h4>Best 매핑 결과</h4>
        <p>crossing_edge: <code class="mono">{row.get('mapped_crossing_edge_id', '–')}</code></p>
        {orig_edge_note}
        <p>junction: <code class="mono">{row.get('mapped_junction_id', '–')}</code></p>
        <p>TLS 존재: <b>{'✓' if row.get('mapped_has_tls') else '✗'}</b></p>
        <p>nearest TLS: <code>{row.get('tls_id_nearest', '–')}</code>  ({fmt(tls_dist, 1)}m)</p>
        <p>mapped lat: <b>{fmt(row.get('mapped_lat'), 6)}</b></p>
        <p>mapped lon: <b>{fmt(row.get('mapped_lon'), 6)}</b></p>
        <p>crossing 거리: <b>{fmt(dist, 1)}m</b></p>
        <p class="reason">{row.get('mapping_reason', '')}</p>
        {'<a class="map-btn mapped" href="' + gmaps_mapped + '" target="_blank">Google Maps 매핑 좌표</a>' if gmaps_mapped else ''}
      </div>

      <div class="col review-col">
        <h4>수동 검토</h4>
        <label>판정</label>
        <select data-id="{cw_id}" class="status-select" onchange="saveStatus('{cw_id}')">
          <option value="">-- 선택 --</option>
          <option value="accept">✅ accept</option>
          <option value="proxy_accept">🟡 proxy_accept</option>
          <option value="needs_review">🔍 needs_review</option>
          <option value="reject">❌ reject</option>
        </select>
        <label>메모</label>
        <textarea data-id="{cw_id}" class="note-input" rows="3"
          placeholder="수동 확인 메모..." onchange="saveStatus('{cw_id}')"></textarea>
      </div>
    </div>

    <details>
      <summary>Top-5 매핑 후보 보기</summary>
      <table class="top5-table">
        <thead>
          <tr><th>순위</th><th>crossing_edge_id</th><th>TLS</th><th>거리</th>
              <th>레벨</th><th>점수</th><th>mapped 좌표</th><th>지도</th></tr>
        </thead>
        <tbody>{top5_rows_html}</tbody>
      </table>
    </details>
  </div>"""
    cards_html.append(card)

# 매핑 레벨 요약
level_counts = audit_df["mapping_level"].value_counts().to_dict()
summary_html = " &nbsp;|&nbsp; ".join(
    f'<span class="badge" style="background:{LEVEL_COLOR.get(k, "#888")}">{k}: {v}</span>'
    for k, v in sorted(level_counts.items())
)

HTML = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>generated_signal_7 후보 매핑 리뷰</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f8fafc; color: #1e293b; margin: 0; padding: 20px; }}
h1 {{ font-size: 1.4rem; margin-bottom: 4px; }}
.subtitle {{ color: #64748b; font-size: 0.9rem; margin-bottom: 16px; }}
.summary-bar {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px 16px; margin-bottom: 20px; }}
.filter-bar {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; align-items: center; }}
.filter-bar select, .filter-bar input {{ padding: 6px 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.85rem; }}
.export-btn {{ padding: 7px 16px; background: #3b82f6; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 0.85rem; }}
.export-btn:hover {{ background: #2563eb; }}
.card {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; margin-bottom: 16px; overflow: hidden; }}
.card-header {{ padding: 12px 16px; background: #f1f5f9; }}
.card-title {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
.cw-id {{ font-weight: 700; font-size: 1rem; }}
.dong {{ color: #475569; font-size: 0.9rem; }}
.score {{ color: #64748b; font-size: 0.85rem; }}
.card-meta {{ color: #64748b; font-size: 0.8rem; margin-top: 4px; }}
.card-body {{ display: flex; gap: 0; flex-wrap: wrap; }}
.col {{ flex: 1; min-width: 220px; padding: 14px 16px; border-right: 1px solid #f1f5f9; }}
.col:last-child {{ border-right: none; }}
.col h4 {{ font-size: 0.85rem; color: #64748b; margin: 0 0 8px 0; text-transform: uppercase; letter-spacing: 0.05em; }}
.col p {{ margin: 4px 0; font-size: 0.88rem; }}
.reason {{ color: #64748b; font-size: 0.8rem; font-style: italic; }}
.mono {{ font-family: monospace; font-size: 0.82rem; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; color: #fff; font-size: 0.78rem; font-weight: 600; }}
.tls-badge {{ background: #6366f1; color: #fff; padding: 1px 6px; border-radius: 10px; font-size: 0.75rem; font-weight: 600; }}
.map-btn {{ display: inline-block; margin-top: 8px; padding: 5px 12px; background: #0ea5e9; color: #fff; text-decoration: none; border-radius: 6px; font-size: 0.82rem; }}
.map-btn.mapped {{ background: #10b981; }}
.map-btn:hover {{ opacity: 0.85; }}
.review-col {{ background: #fafafa; }}
.status-select {{ width: 100%; margin-top: 4px; margin-bottom: 8px; padding: 5px 8px; border-radius: 5px; border: 1px solid #cbd5e1; font-size: 0.85rem; }}
.note-input {{ width: 100%; padding: 6px 8px; border: 1px solid #cbd5e1; border-radius: 5px; font-size: 0.82rem; resize: vertical; }}
details {{ padding: 0 16px 12px; }}
summary {{ cursor: pointer; color: #3b82f6; font-size: 0.85rem; padding: 8px 0 4px; }}
.top5-table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; margin-top: 6px; }}
.top5-table th {{ background: #f1f5f9; padding: 5px 8px; text-align: left; border-bottom: 1px solid #e2e8f0; }}
.top5-table td {{ padding: 4px 8px; border-bottom: 1px solid #f1f5f9; }}
.top5-table a {{ color: #3b82f6; text-decoration: none; }}
.hidden {{ display: none !important; }}
</style>
</head>
<body>

<h1>generated_signal_7 후보 매핑 리뷰</h1>
<p class="subtitle">net: generated_signal_7.net.xml &nbsp;|&nbsp; crossing 21,207개 &nbsp;|&nbsp; 후보 {len(audit_rows)}개 &nbsp;|&nbsp; 생성일: 2026-05-20</p>

<div class="summary-bar">
  <b>매핑 레벨 분포:</b> &nbsp; {summary_html}
</div>

<div class="filter-bar">
  <label>레벨 필터:</label>
  <select id="filter-level" onchange="applyFilter()">
    <option value="">전체</option>
    <option value="exact">exact</option>
    <option value="good">good</option>
    <option value="approx">approx</option>
    <option value="distant">distant</option>
    <option value="far">far</option>
  </select>
  <label>판정 필터:</label>
  <select id="filter-status" onchange="applyFilter()">
    <option value="">전체</option>
    <option value="">미검토</option>
    <option value="accept">accept</option>
    <option value="proxy_accept">proxy_accept</option>
    <option value="needs_review">needs_review</option>
    <option value="reject">reject</option>
  </select>
  <input type="text" id="filter-id" placeholder="crosswalk_id 검색..." oninput="applyFilter()">
  <button class="export-btn" onclick="exportCSV()">결과 CSV 내보내기</button>
</div>

{''.join(cards_html)}

<script>
const reviewData = {{}};

function saveStatus(id) {{
  const sel = document.querySelector(`select[data-id="${{id}}"]`);
  const note = document.querySelector(`textarea[data-id="${{id}}"]`);
  reviewData[id] = {{
    manual_status: sel ? sel.value : '',
    manual_note: note ? note.value : '',
  }};
}}

function applyFilter() {{
  const level = document.getElementById('filter-level').value;
  const status = document.getElementById('filter-status').value;
  const idq = document.getElementById('filter-id').value.toLowerCase();
  document.querySelectorAll('.card').forEach(card => {{
    const badge = card.querySelector('.badge');
    const cardLevel = badge ? badge.textContent.trim() : '';
    const cardId = card.id.replace('card-', '').toLowerCase();
    const sel = card.querySelector('select.status-select');
    const cardStatus = sel ? sel.value : '';
    let show = true;
    if (level && cardLevel !== level) show = false;
    if (status && cardStatus !== status) show = false;
    if (idq && !cardId.includes(idq)) show = false;
    card.classList.toggle('hidden', !show);
  }});
}}

function exportCSV() {{
  const rows = [];
  rows.push(['crosswalk_id','manual_status','manual_note'].join(','));
  document.querySelectorAll('.card').forEach(card => {{
    const id = card.id.replace('card-', '');
    const sel = card.querySelector('select.status-select');
    const note = card.querySelector('textarea.note-input');
    const status = sel ? sel.value : '';
    const noteVal = (note ? note.value : '').replace(/,/g, '；').replace(/\\n/g, ' ');
    rows.push([id, status, noteVal].join(','));
  }});
  const blob = new Blob([rows.join('\\n')], {{type: 'text/csv;charset=utf-8;'}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'generated_signal_7_manual_review_result.csv';
  a.click();
}}
</script>
</body>
</html>"""

HTML_PATH = OUTPUT_DIR / "generated_signal_7_candidate_review.html"
with open(HTML_PATH, "w", encoding="utf-8") as f:
    f.write(HTML)

print(f"  ✓ {HTML_PATH.name}")

# ── 최종 요약 ─────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("완료")
print("=" * 60)
for fpath in [AUDIT_CSV, LONG_CSV, LINK_CSV, HTML_PATH, GEOJSON_PATH]:
    size = fpath.stat().st_size
    print(f"  ✓ {fpath.name}  ({size:,} bytes)")

print("\n매핑 레벨 분포:")
print(audit_df["mapping_level"].value_counts().to_string())
print("\nHTML 리뷰 파일 열기:")
print(f"  open '{HTML_PATH}'")
