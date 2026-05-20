"""
make_top7_sim_candidates.py

확정된 crossing 매핑을 바탕으로 top7_sim 실행에 필요한
single_candidate CSV와 baseline_candidates CSV를 생성한다.
runner와 동일한 _route_pair_from_crossing_edge / _validate_route_pair 로직으로 검증한다.

사용법:
    cd /path/to/js
    python3 analysis/make_top7_sim_candidates.py
    python3 analysis/make_top7_sim_candidates.py --verify-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import sumolib

# ── runner 함수 직접 임포트 (동일 로직 보장) ─────────────────────────────────
_JS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_JS_ROOT))
from smart_crosswalk_sumo.run_phase6_recovery_smoke import (   # noqa: E402
    _route_pair_from_crossing_edge,
    _validate_route_pair,
)

# ── 최종 top7 ID ──────────────────────────────────────────────────────────────
TOP7_FINAL_IDS = [
    "LINK_194891",
    "NODE_10262",
    "NODE_125895",
    "NODE_5831",
    "LINK_239754",
    "NODE_5846",
    "NODE_8369",
]

# ── crossing 수동 오버라이드 (dedup 고정) ─────────────────────────────────────
# 자동 매핑 시 top7 내에서 crossing 중복이 발생하는 후보를 명시적으로 고정.
# NODE_5831: 자동 폴백이 NODE_5846(:5593950705_c0)과 충돌 → 인근 :5161705573_c0
# NODE_125895: 자동 폴백이 NODE_8369(:1936511299_c0)과 충돌 → 더 가까운 :1936511299_c1
# NODE_8369: 위 결과로 :1936511299_c0 단독 배정
CROSSING_OVERRIDES: dict[str, str] = {
    "NODE_5831":   ":5161705573_c0",   # 20.3m – NODE_5846(:5593950705_c0)과 dedup
    "NODE_125895": ":1936511299_c1",   # 6.9m  – NODE_8369(:1936511299_c0)과 dedup
    "NODE_8369":   ":1936511299_c0",   # 17.5m
    # :5593950705_c0은 SUMO 보행자 경로에서 우회됨 → 20.3m의 유효 crossing 사용
    "NODE_5846":   ":7195225083_c0",   # 20.3m, valid pedestrian path 확인됨
}

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
JS_ROOT   = _JS_ROOT
NET_PATH  = JS_ROOT / "result/active/nets/generated_signal_7.net.xml"

MAPPING_A1 = JS_ROOT / "result/osm_audit/generated_signal_7_final_accepted_mapping.csv"
_REVIEW2_CANDIDATES = [
    JS_ROOT / "result/osm_audit/generated_signal_7_review2_result.csv",
    Path.home() / "Downloads/generated_signal_7_review2_result.csv",
]
REVIEW2_CSV = next((p for p in _REVIEW2_CANDIDATES if p.exists()), None)

OUT_DIR    = JS_ROOT / "final/top7_sim/manifests"
SINGLE_DIR = OUT_DIR / "single_candidates"
SINGLE_DIR.mkdir(parents=True, exist_ok=True)

BATCH_NET = "result/active/nets/generated_signal_7.net.xml"

# ── CLI ───────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--verify-only", action="store_true")
args = parser.parse_args()

# ── net 로드 ──────────────────────────────────────────────────────────────────
import re, xml.etree.ElementTree as ET

print("net.xml 로드 중...")
net = sumolib.net.readNet(str(NET_PATH), withInternal=True)

tree_xml = ET.parse(str(NET_PATH))
crossing_to_li: dict[str, int] = {}
tls_junc_crossings: dict[str, list[tuple[str, int]]] = {}
for conn in tree_xml.getroot().findall("connection"):
    to_e = conn.get("to", ""); tl = conn.get("tl", ""); li = conn.get("linkIndex", "")
    if tl and li and re.match(r"^:.+_c\d+$", to_e):
        idx = int(li)
        crossing_to_li[to_e] = idx
        tls_junc_crossings.setdefault(tl, []).append((to_e, idx))

print(f"  crossing→linkIndex: {len(crossing_to_li)}  TLS junctions: {len(tls_junc_crossings)}")


# ── route pair 탐색 (runner와 동일 로직) ──────────────────────────────────────
def find_best_route_pair(crossing_edge_id: str) -> tuple[str, str, str, str]:
    """
    runner의 _route_pair_from_crossing_edge + _validate_route_pair와 동일한 로직으로
    유효한 (from_edge, to_edge, method, reason)을 반환한다.
    유효 쌍이 없으면 ("", "", "none", reason)을 반환한다.
    """
    try:
        candidates = _route_pair_from_crossing_edge(net, crossing_edge_id)
    except Exception as e:
        return "", "", "none", f"_route_pair_from_crossing_edge failed: {e}"

    for frm, to, method in candidates:
        valid, reason, _ = _validate_route_pair(net, crossing_edge_id, frm, to)
        if valid:
            return frm, to, method, "ok"

    # 실패 시 마지막 이유 반환
    last_reason = "no_valid_pair_from_all_candidates"
    for frm, to, method in candidates[-1:]:
        _, last_reason, _ = _validate_route_pair(net, crossing_edge_id, frm, to)
    return "", "", "none", last_reason


# ── verify-only 모드 ──────────────────────────────────────────────────────────
if args.verify_only:
    print("\n=== --verify-only: runner 동등 검증 ===")
    all_ok = True

    for cw in TOP7_FINAL_IDS:
        p = SINGLE_DIR / f"{cw}.csv"
        if not p.exists():
            print(f"  ✗ {cw}: CSV 없음"); all_ok = False; continue
        r = pd.read_csv(p).iloc[0]
        crossing = str(r.get("crossing_edge_id", ""))
        frm      = str(r.get("route_from_edge", ""))
        to       = str(r.get("route_to_edge", ""))

        valid, reason, path_ids = _validate_route_pair(net, crossing, frm, to)
        mark = "✓" if valid else "✗"
        path_preview = "→".join(path_ids[:3]) + ("…" if len(path_ids) > 3 else "") if path_ids else ""
        print(f"  {mark} {cw:15s}  crossing={crossing:25s}  {reason}  {path_preview}")
        if not valid:
            all_ok = False

    bp = OUT_DIR / "top7_baseline_candidates.csv"
    if bp.exists():
        bdf   = pd.read_csv(bp)
        extra = set(bdf["crosswalk_id"].astype(str)) - set(TOP7_FINAL_IDS)
        miss  = set(TOP7_FINAL_IDS) - set(bdf["crosswalk_id"].astype(str))
        if extra or miss:
            print(f"\n  ⚠ baseline CSV 이상: extra={extra}  missing={miss}"); all_ok = False
        else:
            print(f"\n  ✓ baseline CSV: top7 {len(bdf)}행")
    else:
        print(f"\n  ✗ baseline CSV 없음"); all_ok = False

    sys.exit(0 if all_ok else 1)


# ── 확정 매핑 로드 ─────────────────────────────────────────────────────────────
print("\n확정 매핑 로드 중...")
a1 = pd.read_csv(MAPPING_A1)
a1["crosswalk_id"] = a1["crosswalk_id"].astype(str)

r2_frames = []
if REVIEW2_CSV and REVIEW2_CSV.exists():
    r2 = pd.read_csv(REVIEW2_CSV).rename(columns={"crossing_edge_id": "mapped_crossing_edge_id"})
    r2["crosswalk_id"] = r2["crosswalk_id"].astype(str)
    r2_frames.append(r2[["crosswalk_id", "mapped_crossing_edge_id"]])
    print(f"  review2: {REVIEW2_CSV.name} ({len(r2)}행)")
else:
    print("  ⚠ review2 CSV 없음")

all_map = pd.concat(
    [a1[["crosswalk_id", "mapped_crossing_edge_id", "tls_id_nearest"]]] + r2_frames,
    ignore_index=True,
)

for idx, row in all_map.iterrows():
    tls_val = str(row.get("tls_id_nearest", ""))
    if not tls_val or tls_val == "nan":
        m = re.match(r"^:(.+)_c\d+$", str(row["mapped_crossing_edge_id"]))
        if m:
            all_map.at[idx, "tls_id_nearest"] = m.group(1)

print(f"  총 후보: {len(all_map)}")


# ── candidate CSV 생성 ────────────────────────────────────────────────────────
print("\ncandidate CSV 생성 중...")

results       = []
resolution    = []   # top7_mapping_resolution.csv 용

for _, row in all_map.iterrows():
    cw_id          = str(row["crosswalk_id"])
    original_cross = str(row["mapped_crossing_edge_id"])

    # tls_id 정규화
    tls_id = str(row.get("tls_id_nearest", "")).strip()
    if tls_id and tls_id != "nan":
        try: tls_id = str(int(float(tls_id)))
        except ValueError: pass
    if not tls_id or tls_id == "nan":
        m = re.match(r"^:(.+)_c\d+$", original_cross)
        tls_id = m.group(1) if m else ""

    # ── 수동 crossing 오버라이드 우선 적용 ────────────────────────────────────
    if cw_id in CROSSING_OVERRIDES:
        final_crossing   = CROSSING_OVERRIDES[cw_id]
        ped_link_index   = crossing_to_li.get(final_crossing, 0)
        m_junc           = re.match(r"^:(.+)_c\d+$", final_crossing)
        tls_from_cross   = m_junc.group(1) if m_junc else tls_id
        mapping_action   = "FIXED_CROSSING_OVERRIDE"
    else:
        final_crossing   = original_cross
        ped_link_index   = crossing_to_li.get(final_crossing)
        mapping_action   = "AUTO"

        if ped_link_index is None:
            tls_cands = tls_junc_crossings.get(tls_id, [])
            if tls_cands:
                final_crossing, ped_link_index = min(tls_cands, key=lambda x: x[1])
                mapping_action = "FIXED_ROUTE_PAIR"  # TLS 폴백
            else:
                ped_link_index = 0
                mapping_action = "FALLBACK_LI_ZERO"

        m_junc = re.match(r"^:(.+)_c\d+$", final_crossing)
        tls_from_cross = m_junc.group(1) if m_junc else tls_id

    # ── route pair: runner와 동일 로직 ────────────────────────────────────────
    from_edge, to_edge, route_method, route_reason = find_best_route_pair(final_crossing)

    route_ok = (route_reason == "ok")

    if route_ok:
        verdict = "TOP7_SIM_READY"
        if mapping_action == "AUTO":
            mapping_status = "READY"
        else:
            mapping_status = mapping_action
    else:
        verdict = "EXCLUDE_NO_VALID_PED_ROUTE"
        mapping_status = "EXCLUDE_NO_VALID_PED_ROUTE"

    out = {
        "crosswalk_id":             cw_id,
        "source_crosswalk_id":      cw_id,
        "canonical_crosswalk_id":   cw_id,
        "nearest_junction_id":      tls_from_cross,
        "tls_id_used":              tls_from_cross,
        "crossing_id":              final_crossing,
        "crossing_edge_id":         final_crossing,
        "ped_link_index":           ped_link_index,
        "ped_link_indices":         f"[{ped_link_index}]",
        "ped_depart_offset_sec":    0.0,
        "ped_repeat_count":         5,
        "ped_repeat_spacing_sec":   1.5,
        "route_from_edge":          from_edge,
        "route_to_edge":            to_edge,
        "source_file":              "top7_sim_mapping_audit",
        "batch_network_file":       BATCH_NET,
        "final_verdict":            verdict,
        "preflight_ok":             route_ok,
        "preflight_msg":            route_reason,
        "step_test_ok":             route_ok,
        "controlled_links_count":   "",
        "crossing_inventory_used":  False,
    }
    results.append(out)
    pd.DataFrame([out]).to_csv(SINGLE_DIR / f"{cw_id}.csv", index=False, encoding="utf-8-sig")

    # resolution row
    resolution.append({
        "crosswalk_id":              cw_id,
        "original_crossing_edge_id": original_cross,
        "final_crossing_edge_id":    final_crossing,
        "tls_id_used":               tls_from_cross,
        "ped_link_index":            ped_link_index,
        "route_from_edge":           from_edge,
        "route_to_edge":             to_edge,
        "route_validation_ok":       route_ok,
        "route_validation_reason":   route_reason,
        "route_method":              route_method,
        "mapping_status":            mapping_status,
        "mapping_action":            mapping_action,
        "is_top7":                   cw_id in TOP7_FINAL_IDS,
        "is_selected_for_final_sim": cw_id in TOP7_FINAL_IDS and route_ok,
    })

print(f"  생성: {len(results)}개")

# ── preflight 결과 출력 ───────────────────────────────────────────────────────
print("\n=== preflight (runner 동등 기준) ===")
top7_ok_count = 0
for res in resolution:
    if not res["is_top7"]:
        continue
    mark = "✓" if res["route_validation_ok"] else "✗"
    print(f"  {mark} {res['crosswalk_id']:15s}  "
          f"crossing={res['final_crossing_edge_id']:25s}  "
          f"route={res['route_from_edge']}→{res['route_to_edge']}  "
          f"[{res['route_method']}]  {res['route_validation_reason']}")
    if res["route_validation_ok"]:
        top7_ok_count += 1

excluded = [r for r in resolution if r["is_top7"] and not r["route_validation_ok"]]
if excluded:
    print(f"\n  ⚠ top7 내 route 실패 {len(excluded)}개: {[r['crosswalk_id'] for r in excluded]}")
    print("    → EXCLUDE_NO_VALID_PED_ROUTE로 표시, baseline CSV 제외")

# ── baseline CSV: top7 통과 후보만 ────────────────────────────────────────────
results_df   = pd.DataFrame(results)
top7_valid   = results_df[
    results_df["crosswalk_id"].isin(TOP7_FINAL_IDS) &
    results_df["preflight_ok"]
].copy()

baseline_path = OUT_DIR / "top7_baseline_candidates.csv"
top7_valid.to_csv(baseline_path, index=False, encoding="utf-8-sig")
print(f"\n✓ baseline CSV: {len(top7_valid)}행  ({baseline_path.name})")
print(f"  포함: {top7_valid['crosswalk_id'].tolist()}")
if not (set(top7_valid["crosswalk_id"]) - set(TOP7_FINAL_IDS)):
    print("✓ baseline CSV에 top7 외 ID 없음")

# ── mapping resolution CSV 저장 ───────────────────────────────────────────────
resolution_df = pd.DataFrame(resolution)
resolution_path = OUT_DIR / "top7_mapping_resolution.csv"
resolution_df.to_csv(resolution_path, index=False, encoding="utf-8-sig")
print(f"✓ resolution CSV: {resolution_path.name}  ({len(resolution_df)}행)")

# ── 최종 요약 ─────────────────────────────────────────────────────────────────
print(f"\n=== 최종 요약 ===")
print(f"  top7 route 검증 통과: {top7_ok_count}/7")
for status, cnt in resolution_df[resolution_df["is_top7"]]["mapping_status"].value_counts().items():
    print(f"  {status}: {cnt}")
