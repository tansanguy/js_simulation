"""
make_review2.py

1차 수작업 결과를 반영해서:
 - accept (중복 없음) → final_accepted_mapping.csv 저장
 - reject → 대체 crossing 후보 10개를 보여주는 2차 HTML 생성

사용법:
    cd /Users/junlee/Desktop/2026-1/js
    python3 analysis/make_review2.py
"""

from __future__ import annotations
import json, math
from pathlib import Path
import pandas as pd

JS_ROOT = Path(__file__).resolve().parent.parent
AUDIT_CSV   = JS_ROOT / "result/osm_audit/generated_signal_7_all_candidate_mapping_audit.csv"
LONG_CSV    = JS_ROOT / "result/osm_audit/generated_signal_7_candidate_mapping_candidates_long.csv"
REVIEW_CSV  = Path("/Users/junlee/Downloads/generated_signal_7_manual_review_result (1).csv")
OUTPUT_DIR  = JS_ROOT / "result/osm_audit"

NET_PATH = JS_ROOT / "result/active/nets/generated_signal_7.net.xml"

# ── 데이터 로드 ───────────────────────────────────────────────────────────────
review = pd.read_csv(REVIEW_CSV)
review["crosswalk_id"] = review["crosswalk_id"].astype(str)

audit = pd.read_csv(AUDIT_CSV)
audit["crosswalk_id"] = audit["crosswalk_id"].astype(str)

long = pd.read_csv(LONG_CSV)
long["crosswalk_id"] = long["crosswalk_id"].astype(str)

merged = audit.merge(review, on="crosswalk_id", how="left")

# ── accept 확정 ───────────────────────────────────────────────────────────────
accept_ids  = review[review["manual_status"] == "accept"]["crosswalk_id"].tolist()
reject_ids  = review[review["manual_status"] == "reject"]["crosswalk_id"].tolist()
other_ids   = review[~review["manual_status"].isin(["accept","reject"])]["crosswalk_id"].tolist()

accepted_df = merged[merged["crosswalk_id"].isin(accept_ids)].copy()
dup_mask    = accepted_df.duplicated("mapped_crossing_edge_id", keep=False)
clean_accept = accepted_df[~dup_mask]
dup_accept   = accepted_df[dup_mask]

print(f"accept : {len(accept_ids)}개  (중복 crossing: {len(dup_accept)}개)")
print(f"reject : {len(reject_ids)}개  → 2차 HTML 대상")
print(f"기타   : {len(other_ids)}개")

# final accepted CSV 저장
FINAL_CSV = OUTPUT_DIR / "generated_signal_7_final_accepted_mapping.csv"
save_cols = [c for c in [
    "crosswalk_id","dong_name","candidate_lat","candidate_lon",
    "mapped_crossing_edge_id","mapped_junction_id","mapped_has_tls",
    "mapped_lat","mapped_lon","crossing_distance_m",
    "tls_id_nearest","tls_distance_m",
    "mapping_score","mapping_level","mapping_reason","was_reassigned",
    "net_group","risk_rank","risk_score","accident_count",
    "manual_status","manual_note",
] if c in clean_accept.columns]
clean_accept[save_cols].to_csv(FINAL_CSV, index=False, encoding="utf-8-sig")
print(f"\n✓ 확정 매핑 저장: {FINAL_CSV.name}  ({len(clean_accept)}행)")

# ── 2차 HTML 대상: reject + dup_accept + other ────────────────────────────────
review2_ids = reject_ids + dup_accept["crosswalk_id"].tolist() + other_ids
review2_ids = list(dict.fromkeys(review2_ids))  # 순서 유지 dedup

if not review2_ids:
    print("2차 HTML 대상 없음.")
    raise SystemExit(0)

print(f"\n2차 HTML 대상: {review2_ids}")

# 해당 후보들의 top-10 대체 후보 (long CSV)
# long CSV는 TOP_K=10으로 생성됐으므로 그대로 사용
long2 = long[long["crosswalk_id"].isin(review2_ids)].copy()

# 이미 1차 accept에서 사용 중인 crossing은 표시
used_crossings = set(clean_accept["mapped_crossing_edge_id"].tolist())

# net.xml 로드 (좌표 역변환용)
print("net.xml 로드 중...")
import sumolib, time
t0 = time.time()
net = sumolib.net.readNet(str(NET_PATH), withInternal=True)
print(f"  완료 ({time.time()-t0:.1f}s)")

# ── HTML 생성 ─────────────────────────────────────────────────────────────────
LEVEL_COLOR = {
    "exact":   "#22c55e",
    "good":    "#84cc16",
    "approx":  "#f59e0b",
    "distant": "#ef4444",
    "far":     "#dc2626",
}

def fmt(v, d=2):
    if v is None or (isinstance(v, float) and math.isnan(v)): return "–"
    if isinstance(v, float): return f"{v:.{d}f}"
    return str(v)

cards_html = []

for cw_id in review2_ids:
    base = merged[merged["crosswalk_id"] == cw_id]
    if base.empty:
        continue
    row = base.iloc[0]

    prev_status = row.get("manual_status", "")
    status_label = {
        "reject": "❌ 1차 reject",
        "proxy_accept": "🟡 1차 proxy_accept",
        "needs_review": "🔍 1차 needs_review",
        "": "⬜ 미판정",
    }.get(str(prev_status), str(prev_status))

    # 대체 후보 행들
    alts = long2[long2["crosswalk_id"] == cw_id].sort_values("distance_m")

    alt_rows_html = ""
    for _, alt in alts.iterrows():
        already_used = alt["crossing_edge_id"] in used_crossings
        used_badge = '<span style="color:#ef4444;font-weight:700"> ⛔사용중</span>' if already_used else ""
        is_assigned_badge = '<span style="color:#3b82f6;font-weight:700"> ★현재배정</span>' if alt.get("is_assigned") else ""
        lv = alt.get("mapping_level","")
        lv_color = LEVEL_COLOR.get(lv, "#888")
        tls_badge = '<span class="tls-badge">TLS</span>' if alt.get("has_tls") else ""

        gmaps = f"https://www.google.com/maps/search/?api=1&query={alt['mapped_lat']},{alt['mapped_lon']}"

        alt_rows_html += f"""
        <tr class="alt-row {'used-row' if already_used else ''}">
          <td>{int(alt['rank'])}</td>
          <td class="mono">{alt['crossing_edge_id']}{used_badge}{is_assigned_badge}</td>
          <td>{tls_badge}</td>
          <td><b>{fmt(alt['distance_m'],1)}m</b></td>
          <td><span class="badge" style="background:{lv_color}">{lv}</span></td>
          <td>{int(alt['mapping_score'])}</td>
          <td>{fmt(alt['mapped_lat'],6)}, {fmt(alt['mapped_lon'],6)}</td>
          <td><a href="{gmaps}" target="_blank">지도</a></td>
          <td>
            <button class="pick-btn" onclick="pickAlt('{cw_id}','{alt['crossing_edge_id']}',
              '{fmt(alt['mapped_lat'],6)}','{fmt(alt['mapped_lon'],6)}',
              '{lv}',{int(alt['mapping_score'])})">
              선택
            </button>
          </td>
        </tr>"""

    cand_lat = fmt(row.get("candidate_lat"), 6)
    cand_lon = fmt(row.get("candidate_lon"), 6)
    gmaps_cand = f"https://www.google.com/maps/search/?api=1&query={cand_lat},{cand_lon}"

    cards_html.append(f"""
  <div class="card" id="card-{cw_id}">
    <div class="card-header">
      <div class="card-title">
        <span class="cw-id">{cw_id}</span>
        <span class="dong">{row.get('dong_name','')}</span>
        <span class="prev-status">{status_label}</span>
      </div>
      <div class="card-meta">
        원좌표: {cand_lat}, {cand_lon} &nbsp;
        <a href="{gmaps_cand}" target="_blank" class="map-btn">Google Maps 원좌표</a>
        &nbsp;|&nbsp; 네이버/카카오 검색: <code>{cand_lat},{cand_lon}</code>
      </div>
    </div>

    <div class="card-body">
      <div class="result-area" id="result-{cw_id}">
        <span style="color:#94a3b8">← 아래 표에서 crossing을 선택하세요</span>
      </div>

      <table class="alt-table">
        <thead>
          <tr>
            <th>순위</th><th>crossing_edge_id</th><th>TLS</th>
            <th>거리</th><th>레벨</th><th>점수</th>
            <th>mapped 좌표</th><th>지도</th><th>선택</th>
          </tr>
        </thead>
        <tbody>{alt_rows_html}</tbody>
      </table>

      <div class="note-row">
        <label>메모</label>
        <input type="text" id="note-{cw_id}" placeholder="수동 메모..." style="width:60%;padding:5px 8px;border:1px solid #cbd5e1;border-radius:5px;font-size:0.85rem;">
        <button class="export-btn" onclick="exportOne('{cw_id}')">이 항목 확정</button>
      </div>
    </div>
  </div>""")

HTML = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>2차 리뷰 – generated_signal_7</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f8fafc; color: #1e293b; margin: 0; padding: 20px; }}
h1 {{ font-size: 1.3rem; margin-bottom: 4px; }}
.subtitle {{ color:#64748b; font-size:0.88rem; margin-bottom:16px; }}
.top-bar {{ display:flex; gap:10px; align-items:center; margin-bottom:16px; flex-wrap:wrap; }}
.export-btn {{ padding:7px 16px; background:#3b82f6; color:#fff; border:none; border-radius:6px; cursor:pointer; font-size:0.85rem; }}
.export-btn:hover {{ background:#2563eb; }}
.export-all-btn {{ padding:7px 20px; background:#10b981; color:#fff; border:none; border-radius:8px; cursor:pointer; font-size:0.9rem; font-weight:600; }}
.export-all-btn:hover {{ background:#059669; }}
.card {{ background:#fff; border:1px solid #e2e8f0; border-radius:10px; margin-bottom:18px; overflow:hidden; }}
.card-header {{ padding:12px 16px; background:#f1f5f9; border-bottom:1px solid #e2e8f0; }}
.card-title {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
.cw-id {{ font-weight:700; font-size:1rem; }}
.dong {{ color:#475569; font-size:0.9rem; }}
.prev-status {{ font-size:0.82rem; color:#64748b; }}
.card-meta {{ margin-top:6px; font-size:0.82rem; color:#475569; }}
.card-body {{ padding:14px 16px; }}
.result-area {{ margin-bottom:10px; padding:10px 14px; background:#f0fdf4; border:1px solid #bbf7d0; border-radius:8px; font-size:0.88rem; min-height:40px; }}
.result-area.picked {{ background:#eff6ff; border-color:#bfdbfe; }}
.alt-table {{ width:100%; border-collapse:collapse; font-size:0.82rem; margin-bottom:10px; }}
.alt-table th {{ background:#f1f5f9; padding:5px 8px; text-align:left; border-bottom:1px solid #e2e8f0; white-space:nowrap; }}
.alt-table td {{ padding:5px 8px; border-bottom:1px solid #f8fafc; }}
.alt-table a {{ color:#3b82f6; text-decoration:none; }}
.used-row td {{ color:#94a3b8; }}
.badge {{ display:inline-block; padding:2px 7px; border-radius:10px; color:#fff; font-size:0.75rem; font-weight:600; }}
.tls-badge {{ background:#6366f1; color:#fff; padding:1px 5px; border-radius:8px; font-size:0.73rem; font-weight:600; }}
.mono {{ font-family:monospace; font-size:0.8rem; }}
.map-btn {{ padding:3px 10px; background:#0ea5e9; color:#fff; text-decoration:none; border-radius:5px; font-size:0.8rem; }}
.pick-btn {{ padding:3px 10px; background:#6366f1; color:#fff; border:none; border-radius:5px; cursor:pointer; font-size:0.8rem; }}
.pick-btn:hover {{ background:#4f46e5; }}
.note-row {{ display:flex; gap:8px; align-items:center; margin-top:6px; flex-wrap:wrap; }}
.note-row label {{ font-size:0.82rem; color:#64748b; white-space:nowrap; }}
</style>
</head>
<body>

<h1>2차 매핑 리뷰 – generated_signal_7</h1>
<p class="subtitle">
  1차 reject {len(reject_ids)}개{'  |  중복 accept ' + str(len(dup_accept)) + '개' if not dup_accept.empty else ''} 대상 &nbsp;|&nbsp;
  ⛔ 표시 = 이미 1차 accept에서 사용 중인 crossing &nbsp;|&nbsp;
  ★ 표시 = 현재 자동배정 crossing
</p>

<div class="top-bar">
  <button class="export-all-btn" onclick="exportAll()">전체 선택 결과 CSV 내보내기</button>
</div>

{''.join(cards_html)}

<script>
const picks = {{}};

function pickAlt(cwId, edgeId, lat, lon, level, score) {{
  picks[cwId] = {{ crossing_edge_id: edgeId, mapped_lat: lat, mapped_lon: lon,
                   mapping_level: level, mapping_score: score }};
  const area = document.getElementById('result-' + cwId);
  const note = document.getElementById('note-' + cwId);
  const gmaps = `https://www.google.com/maps/search/?api=1&query=${{lat}},${{lon}}`;
  area.className = 'result-area picked';
  area.innerHTML = `✅ 선택됨: <b>${{edgeId}}</b> | ${{level}} | ${{score}}점 | ${{lat}}, ${{lon}}
    &nbsp; <a href="${{gmaps}}" target="_blank" style="color:#2563eb">Google Maps 확인</a>`;
}}

function exportOne(cwId) {{
  const p = picks[cwId];
  if (!p) {{ alert('먼저 crossing을 선택하세요.'); return; }}
  const note = document.getElementById('note-' + cwId)?.value || '';
  const row = [cwId, p.crossing_edge_id, p.mapped_lat, p.mapped_lon,
               p.mapping_level, p.mapping_score, 'accept_review2', note].join(',');
  const blob = new Blob([
    'crosswalk_id,crossing_edge_id,mapped_lat,mapped_lon,mapping_level,mapping_score,manual_status,manual_note\\n' + row
  ], {{type:'text/csv;charset=utf-8;'}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = `review2_${{cwId}}.csv`; a.click();
}}

function exportAll() {{
  const header = 'crosswalk_id,crossing_edge_id,mapped_lat,mapped_lon,mapping_level,mapping_score,manual_status,manual_note';
  const rows = [header];
  document.querySelectorAll('.card').forEach(card => {{
    const cwId = card.id.replace('card-', '');
    const p = picks[cwId];
    const note = (document.getElementById('note-' + cwId)?.value || '').replace(/,/g,'；');
    if (p) {{
      rows.push([cwId, p.crossing_edge_id, p.mapped_lat, p.mapped_lon,
                 p.mapping_level, p.mapping_score, 'accept_review2', note].join(','));
    }} else {{
      rows.push([cwId, '', '', '', '', '', 'pending', note].join(','));
    }}
  }});
  const blob = new Blob([rows.join('\\n')], {{type:'text/csv;charset=utf-8;'}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = 'generated_signal_7_review2_result.csv'; a.click();
}}
</script>
</body>
</html>"""

HTML2_PATH = OUTPUT_DIR / "generated_signal_7_candidate_review2.html"
with open(HTML2_PATH, "w", encoding="utf-8") as f:
    f.write(HTML)

print(f"✓ 2차 HTML 저장: {HTML2_PATH}")
print(f"  open '{HTML2_PATH}'")
