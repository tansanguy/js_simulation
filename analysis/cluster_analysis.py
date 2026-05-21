"""
cluster_analysis.py

전체 확정 매핑 후보의 지리적 군집을 분석하고
사람이 검토할 수 있는 군집 정리 HTML을 생성한다.

출력:
  result/osm_audit/generated_signal_7_cluster_report.html
  result/osm_audit/generated_signal_7_cluster_summary.csv
"""

from __future__ import annotations
import math
from collections import defaultdict
from itertools import combinations
from pathlib import Path
import pandas as pd

JS_ROOT   = Path(__file__).resolve().parent.parent
OUTPUT_DIR = JS_ROOT / "result/osm_audit"

# ── 데이터 합치기 ─────────────────────────────────────────────────────────────
a1 = pd.read_csv(OUTPUT_DIR / "generated_signal_7_final_accepted_mapping.csv")
r2 = pd.read_csv("/Users/junlee/Downloads/generated_signal_7_review2_result.csv")
r2 = r2.rename(columns={"crossing_edge_id": "mapped_crossing_edge_id"})

meta = pd.read_csv(JS_ROOT / "final/manifests/candidate_metadata.csv")
meta["crosswalk_id"] = meta["crosswalk_id"].astype(str)

try:
    step = pd.read_csv(JS_ROOT / "crosswalk_stepwise_result_50m.csv", encoding="cp949")
    step["crosswalk_id"] = step["crosswalk_id"].astype(str)
    coord_map = {r["crosswalk_id"]: (r["lat"], r["lon"]) for _, r in step.iterrows()}
    dong_map  = {r["crosswalk_id"]: r.get("dong_name","") for _, r in step.iterrows()}
except Exception:
    coord_map, dong_map = {}, {}

rows = []
for _, r in a1.iterrows():
    rows.append(dict(
        crosswalk_id=str(r["crosswalk_id"]),
        mapped_crossing_edge_id=r.get("mapped_crossing_edge_id",""),
        mapped_lat=r.get("mapped_lat"), mapped_lon=r.get("mapped_lon"),
        candidate_lat=r.get("candidate_lat"), candidate_lon=r.get("candidate_lon"),
        mapping_level=r.get("mapping_level",""), mapping_score=r.get("mapping_score",0),
        dong_name=r.get("dong_name",""), manual_status="accept",
    ))
for _, r in r2.iterrows():
    cw = str(r["crosswalk_id"])
    clat, clon = coord_map.get(cw, (None, None))
    rows.append(dict(
        crosswalk_id=cw,
        mapped_crossing_edge_id=r.get("mapped_crossing_edge_id",""),
        mapped_lat=r.get("mapped_lat"), mapped_lon=r.get("mapped_lon"),
        candidate_lat=clat, candidate_lon=clon,
        mapping_level=r.get("mapping_level",""), mapping_score=r.get("mapping_score",0),
        dong_name=dong_map.get(cw,""), manual_status="accept_review2",
    ))

all_df = pd.DataFrame(rows)
all_df = all_df.merge(meta[["crosswalk_id","dong_name","risk_rank","risk_score","net_group"]]
                      .rename(columns={"dong_name":"dong_meta"}), on="crosswalk_id", how="left")
all_df["dong_name"] = all_df["dong_name"].replace("", None).fillna(all_df["dong_meta"])
all_df = all_df.drop(columns=["dong_meta"])

# crossing 중복 플래그
dup_crossings = set(all_df[all_df.duplicated("mapped_crossing_edge_id", keep=False)]["mapped_crossing_edge_id"])

# ── Haversine ─────────────────────────────────────────────────────────────────
def hav(lat1, lon1, lat2, lon2) -> float:
    R = 6371000
    f1, f2 = math.radians(lat1), math.radians(lat2)
    df = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(df/2)**2 + math.cos(f1)*math.cos(f2)*math.sin(dl/2)**2
    return R * 2 * math.asin(math.sqrt(a))

# ── Union-Find 군집화 (200m) ───────────────────────────────────────────────────
THRESH = 200
df_c = all_df.dropna(subset=["candidate_lat","candidate_lon"]).reset_index(drop=True)
ids   = df_c["crosswalk_id"].tolist()
lats  = df_c["candidate_lat"].tolist()
lons  = df_c["candidate_lon"].tolist()

parent = list(range(len(ids)))
def find(x):
    while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
    return x
def union(a, b): parent[find(a)] = find(b)

for i, j in combinations(range(len(ids)), 2):
    if hav(lats[i], lons[i], lats[j], lons[j]) <= THRESH:
        union(i, j)

groups: dict[int, list[int]] = defaultdict(list)
for i in range(len(ids)):
    groups[find(i)].append(i)

clusters = sorted(groups.values(), key=lambda m: -len(m))

# ── 군집별 통계 ───────────────────────────────────────────────────────────────
summary_rows = []
for ci, members in enumerate(clusters, 1):
    sub = df_c.iloc[members]

    # 군집 중심
    clat = sub["candidate_lat"].mean()
    clon = sub["candidate_lon"].mean()

    # 최대 내부 거리
    max_d = 0
    for i, j in combinations(members, 2):
        d = hav(lats[i], lons[i], lats[j], lons[j])
        max_d = max(max_d, d)

    # crossing 중복 여부
    dup_in_cluster = sub["mapped_crossing_edge_id"].isin(dup_crossings).any()

    # 최고 점수 후보
    best_row = sub.loc[sub["mapping_score"].idxmax()]

    summary_rows.append(dict(
        cluster_id=ci,
        n=len(members),
        max_intra_dist_m=round(max_d, 1),
        center_lat=round(clat, 5),
        center_lon=round(clon, 5),
        dong_names=" / ".join(sorted(set(sub["dong_name"].dropna()))),
        crosswalk_ids=", ".join(sub["crosswalk_id"].tolist()),
        best_candidate=best_row["crosswalk_id"],
        best_score=int(best_row["mapping_score"]),
        has_dup_crossing=dup_in_cluster,
        members=members,
        sub=sub,
    ))

# ── HTML 생성 ─────────────────────────────────────────────────────────────────
LEVEL_COLOR = {"exact":"#22c55e","good":"#84cc16","approx":"#f59e0b",
               "distant":"#ef4444","far":"#dc2626"}

def fmt(v, d=2):
    if v is None or (isinstance(v, float) and math.isnan(float(v))): return "–"
    if isinstance(v, float): return f"{v:.{d}f}"
    return str(v)

cards = []
for s in summary_rows:
    ci   = s["cluster_id"]
    sub  = s["sub"]
    n    = s["n"]

    # 군집 단일 여부
    solo = n == 1
    header_color = "#f0fdf4" if solo else ("#fff7ed" if n <= 3 else "#fef2f2")
    border_color = "#22c55e" if solo else ("#f97316" if n <= 3 else "#ef4444")
    tag = "단독" if solo else (f"소군집 {n}개" if n <= 3 else f"⚠ 대군집 {n}개")
    tag_color = "#22c55e" if solo else ("#f97316" if n <= 3 else "#ef4444")

    # Google Maps 군집 중심 링크
    gmaps_center = f"https://www.google.com/maps/search/?api=1&query={s['center_lat']},{s['center_lon']}"

    # 후보별 행
    member_rows = ""
    for _, row in sub.sort_values("mapping_score", ascending=False).iterrows():
        lv     = row.get("mapping_level","")
        lv_col = LEVEL_COLOR.get(lv, "#888")
        is_dup = row["mapped_crossing_edge_id"] in dup_crossings
        dup_badge = '<span style="color:#ef4444;font-weight:700">⛔중복crossing</span>' if is_dup else ""

        # 타 후보와의 거리
        idx = ids.index(row["crosswalk_id"])
        dist_to_others = []
        for j in s["members"]:
            if j == idx: continue
            d = hav(lats[idx], lons[idx], lats[j], lons[j])
            dist_to_others.append(f"{ids[j]}:{d:.0f}m")
        dist_str = " | ".join(dist_to_others) if dist_to_others else "–"

        gmaps_cand = f"https://www.google.com/maps/search/?api=1&query={row['candidate_lat']},{row['candidate_lon']}"
        gmaps_map  = f"https://www.google.com/maps/search/?api=1&query={row['mapped_lat']},{row['mapped_lon']}" if not pd.isna(row.get('mapped_lat','')) else ""

        rr = fmt(row.get("risk_rank",""), 0)
        rs = fmt(row.get("risk_score",""), 2)

        member_rows += f"""
        <tr>
          <td><b>{row['crosswalk_id']}</b></td>
          <td>{row.get('dong_name','')}</td>
          <td><span class="badge" style="background:{lv_col}">{lv}</span></td>
          <td>{int(row['mapping_score'])}</td>
          <td class="mono small">{row['mapped_crossing_edge_id']} {dup_badge}</td>
          <td>{rr}</td>
          <td>{rs}</td>
          <td class="small">{dist_str}</td>
          <td>
            <a href="{gmaps_cand}" target="_blank" class="map-btn">원좌표</a>
            {'<a href="' + gmaps_map + '" target="_blank" class="map-btn green">crossing</a>' if gmaps_map else ''}
          </td>
        </tr>"""

    dup_warning = ""
    if s["has_dup_crossing"]:
        dup_warning = '<div class="warn-box">⛔ 이 군집 내에 동일 crossing에 매핑된 후보가 2개 이상 있습니다. 하나를 선택하거나 재매핑이 필요합니다.</div>'

    cards.append(f"""
  <div class="card">
    <div class="card-header" style="background:{header_color};border-left:5px solid {border_color}">
      <div class="card-title">
        <span class="cluster-num">군집 {ci}</span>
        <span class="cluster-tag" style="background:{tag_color}">{tag}</span>
        <span class="dong-label">{s['dong_names']}</span>
        <span class="dist-label">최대 내부거리 {s['max_intra_dist_m']}m</span>
        <a href="{gmaps_center}" target="_blank" class="map-btn">군집 중심 지도</a>
      </div>
      {dup_warning}
    </div>
    <div class="card-body">
      <table class="member-table">
        <thead>
          <tr>
            <th>crosswalk_id</th><th>동명</th><th>레벨</th><th>점수</th>
            <th>mapped crossing</th><th>위험순위</th><th>risk점수</th>
            <th>군집 내 거리</th><th>지도</th>
          </tr>
        </thead>
        <tbody>{member_rows}</tbody>
      </table>
    </div>
  </div>""")

# 통계 요약
total = len(all_df)
solo_n = sum(1 for s in summary_rows if s["n"] == 1)
small_n = sum(1 for s in summary_rows if 2 <= s["n"] <= 3)
large_n = sum(1 for s in summary_rows if s["n"] > 3)

HTML = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>군집 분석 – generated_signal_7</title>
<style>
* {{ box-sizing:border-box; }}
body {{ font-family:'Segoe UI',Arial,sans-serif; background:#f8fafc; color:#1e293b; margin:0; padding:20px; }}
h1 {{ font-size:1.3rem; margin-bottom:4px; }}
.subtitle {{ color:#64748b; font-size:0.88rem; margin-bottom:16px; }}
.stat-bar {{ display:flex; gap:16px; flex-wrap:wrap; margin-bottom:20px; }}
.stat-box {{ background:#fff; border:1px solid #e2e8f0; border-radius:8px; padding:12px 18px; text-align:center; }}
.stat-num {{ font-size:1.6rem; font-weight:700; }}
.stat-lbl {{ font-size:0.78rem; color:#64748b; margin-top:2px; }}
.card {{ background:#fff; border:1px solid #e2e8f0; border-radius:10px; margin-bottom:16px; overflow:hidden; }}
.card-header {{ padding:12px 16px; }}
.card-title {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
.cluster-num {{ font-weight:700; font-size:1rem; }}
.cluster-tag {{ color:#fff; padding:2px 10px; border-radius:10px; font-size:0.8rem; font-weight:600; }}
.dong-label {{ color:#475569; font-size:0.88rem; }}
.dist-label {{ color:#64748b; font-size:0.82rem; }}
.warn-box {{ margin-top:8px; padding:7px 12px; background:#fef2f2; border:1px solid #fecaca; border-radius:6px; font-size:0.83rem; color:#b91c1c; }}
.card-body {{ padding:0 16px 14px; }}
.member-table {{ width:100%; border-collapse:collapse; font-size:0.82rem; margin-top:10px; }}
.member-table th {{ background:#f1f5f9; padding:5px 8px; text-align:left; border-bottom:1px solid #e2e8f0; white-space:nowrap; }}
.member-table td {{ padding:5px 8px; border-bottom:1px solid #f8fafc; vertical-align:middle; }}
.badge {{ display:inline-block; padding:2px 7px; border-radius:10px; color:#fff; font-size:0.74rem; font-weight:600; }}
.map-btn {{ padding:2px 9px; background:#0ea5e9; color:#fff; text-decoration:none; border-radius:5px; font-size:0.78rem; }}
.map-btn.green {{ background:#10b981; }}
.mono {{ font-family:monospace; }}
.small {{ font-size:0.78rem; }}
</style>
</head>
<body>
<h1>군집 분석 – generated_signal_7 확정 후보 {total}개</h1>
<p class="subtitle">200m 반경 기준 union-find 군집화 &nbsp;|&nbsp; 총 {len(summary_rows)}개 군집</p>

<div class="stat-bar">
  <div class="stat-box"><div class="stat-num" style="color:#22c55e">{solo_n}</div><div class="stat-lbl">단독 (군집 없음)</div></div>
  <div class="stat-box"><div class="stat-num" style="color:#f97316">{small_n}</div><div class="stat-lbl">소군집 (2~3개)</div></div>
  <div class="stat-box"><div class="stat-num" style="color:#ef4444">{large_n}</div><div class="stat-lbl">대군집 (4개 이상)</div></div>
  <div class="stat-box"><div class="stat-num">{len(summary_rows)}</div><div class="stat-lbl">총 군집 수</div></div>
</div>

{''.join(cards)}
</body>
</html>"""

HTML_PATH = OUTPUT_DIR / "generated_signal_7_cluster_report.html"
with open(HTML_PATH, "w", encoding="utf-8") as f:
    f.write(HTML)

# CSV 저장
csv_rows = []
for s in summary_rows:
    for _, row in s["sub"].iterrows():
        csv_rows.append(dict(
            cluster_id=s["cluster_id"],
            cluster_size=s["n"],
            cluster_dong=s["dong_names"],
            max_intra_dist_m=s["max_intra_dist_m"],
            crosswalk_id=row["crosswalk_id"],
            dong_name=row.get("dong_name",""),
            mapped_crossing_edge_id=row["mapped_crossing_edge_id"],
            mapped_lat=row.get("mapped_lat"),
            mapped_lon=row.get("mapped_lon"),
            candidate_lat=row.get("candidate_lat"),
            candidate_lon=row.get("candidate_lon"),
            mapping_level=row.get("mapping_level",""),
            mapping_score=row.get("mapping_score",0),
            risk_rank=row.get("risk_rank",""),
            risk_score=row.get("risk_score",""),
            has_dup_crossing=row["mapped_crossing_edge_id"] in dup_crossings,
        ))
pd.DataFrame(csv_rows).to_csv(
    OUTPUT_DIR / "generated_signal_7_cluster_summary.csv",
    index=False, encoding="utf-8-sig"
)

print(f"✓ HTML: {HTML_PATH}")
print(f"✓ CSV : {OUTPUT_DIR / 'generated_signal_7_cluster_summary.csv'}")
print(f"\n군집 요약:")
for s in summary_rows:
    dup_flag = " ⛔중복" if s["has_dup_crossing"] else ""
    ids_str = ", ".join(s["sub"]["crosswalk_id"].tolist())
    print(f"  군집{s['cluster_id']} ({s['n']}개, {s['max_intra_dist_m']}m) [{s['dong_names']}]{dup_flag}")
    print(f"    {ids_str}")
