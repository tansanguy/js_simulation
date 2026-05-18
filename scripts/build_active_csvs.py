#!/usr/bin/env python3
"""
build_active_csvs.py
Build result/active/csv/* from source candidate tables and top50 base CSV.
No simulations, no SUMO runs.
# 2026-05-16: p1_p4_recovery_7 -> p1_p4_recovery_6 (NODE_5830 dropped)
"""

import csv
import os
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
ACTIVE_CSV = BASE / "result/active/csv"
DATA_ACTIVE = BASE / "data/active"
ACTIVE_CSV.mkdir(parents=True, exist_ok=True)
DATA_ACTIVE.mkdir(parents=True, exist_ok=True)

# ─── Known candidate lists ────────────────────────────────────────────────────

# current_main_12: IDs from main_verified_current_net_12.csv
CURRENT_MAIN_12_IDS = [
    "NODE_10335", "NODE_8369", "NODE_167173", "LINK_239754",
    "NODE_5846", "NODE_5831", "NODE_10377", "NODE_10376",
    "NODE_150723", "NODE_125895", "NODE_10381", "LINK_120139",
]

# signal_fix_9: IDs from signal_fix_candidate_table.csv
SIGNAL_FIX_9_IDS = [
    "LINK_249048", "NODE_5837", "LINK_10218", "LINK_140740",
    "NODE_9634", "LINK_194891", "NODE_10378", "NODE_5681", "NODE_10262",
]

# generated_signal_7: IDs from generated_signal_candidate_table.csv
GENERATED_SIGNAL_7_IDS = [
    "LINK_212169", "NODE_8452", "NODE_6082", "NODE_6123",
    "NODE_8426", "NODE_74388", "NODE_10273",
]

# p1_p4_recovery_6: P1×5 + P4×1 (NODE_5938 only; NODE_5830 dropped)
P1_P4_RECOVERY_6_IDS = [
    "NODE_10060", "NODE_122781", "NODE_14937", "NODE_5647",
    "NODE_6342",
    "NODE_5938",  # P4 approved (NODE_5830 dropped)
]

ALL_ACTIVE_IDS = (
    [(cid, "current_main_12") for cid in CURRENT_MAIN_12_IDS]
    + [(cid, "signal_fix_9") for cid in SIGNAL_FIX_9_IDS]
    + [(cid, "generated_signal_7") for cid in GENERATED_SIGNAL_7_IDS]
    + [(cid, "p1_p4_recovery_6") for cid in P1_P4_RECOVERY_6_IDS]
)

NET_META = {
    "current_main_12": {
        "net_file": "result/active/nets/current_main_12.net.xml",
        "candidate_csv": "result/active/csv/current_main_12_candidates.csv",
        "recovery_type": "batch_tls_recovery",
        "crosswalk_ids": CURRENT_MAIN_12_IDS,
    },
    "signal_fix_9": {
        "net_file": "result/active/nets/signal_fix_9.net.xml",
        "candidate_csv": "result/active/csv/signal_fix_9_candidates.csv",
        "recovery_type": "signal_fix_recovery",
        "crosswalk_ids": SIGNAL_FIX_9_IDS,
    },
    "generated_signal_7": {
        "net_file": "result/active/nets/generated_signal_7.net.xml",
        "candidate_csv": "result/active/csv/generated_signal_7_candidates.csv",
        "recovery_type": "generated_signal_expansion",
        "crosswalk_ids": GENERATED_SIGNAL_7_IDS,
    },
    "p1_p4_recovery_6": {
        "net_file": "result/active/nets/p1_p4_recovery_6.net.xml",
        "candidate_csv": "result/active/csv/p1_p4_recovery_6_candidates.csv",
        "recovery_type": "p1_signal_fix_plus_p4_approved",
        "crosswalk_ids": P1_P4_RECOVERY_6_IDS,
    },
}

CANDIDATE_COLS = [
    "net_group", "crosswalk_id", "source_crosswalk_id", "original_rank",
    "net_file", "candidate_csv", "implemented", "included_in_30seed",
    "recovery_type", "final_status", "note",
]

# ─── Load top50 base CSV ──────────────────────────────────────────────────────
TOP50_SRC = BASE / "data/crosswalk_stepwise_result_50m.csv"
top50_map = {}  # crosswalk_id -> row dict
top50_sorted = []

try:
    with open(TOP50_SRC, "r", encoding="cp949") as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)
    # Sort by risk_score desc
    sortable = [r for r in all_rows if r.get("risk_score", "").strip()]
    sortable.sort(key=lambda x: float(x["risk_score"]), reverse=True)
    top50_sorted = sortable[:50]
    top50_map = {r["crosswalk_id"]: (i + 1, r) for i, r in enumerate(top50_sorted)}
    print(f"[OK] Loaded top50 base CSV: {len(all_rows)} total rows, top50 selected")
except Exception as e:
    print(f"[WARN] Could not load top50 base CSV: {e}")

# Build original_rank lookup from all active IDs
def get_rank(cid):
    if cid in top50_map:
        return top50_map[cid][0]
    return "N/A"


def get_note(cid, net_group):
    notes = []
    if net_group == "p1_p4_recovery_6":
        if cid == "NODE_5938":
            notes.append("P4 approved")
        else:
            notes.append("P1 signal_fix recovery")
    if cid not in top50_map:
        notes.append("not_in_top50_by_risk_score")
    return "; ".join(notes) if notes else ""


# ─── C: Write per-net candidate CSVs ─────────────────────────────────────────
all_rows_out = []

for net_group, meta in NET_META.items():
    out_path = BASE / meta["candidate_csv"]
    rows_out = []
    for cid in meta["crosswalk_ids"]:
        row = {
            "net_group": net_group,
            "crosswalk_id": cid,
            "source_crosswalk_id": cid,
            "original_rank": get_rank(cid),
            "net_file": meta["net_file"],
            "candidate_csv": meta["candidate_csv"],
            "implemented": "False",
            "included_in_30seed": "False",
            "recovery_type": meta["recovery_type"],
            "final_status": "CANDIDATE_TABLE_READY",
            "note": get_note(cid, net_group),
        }
        rows_out.append(row)
        all_rows_out.append(row)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CANDIDATE_COLS)
        writer.writeheader()
        writer.writerows(rows_out)
    print(f"[OK] {out_path.name}: {len(rows_out)} rows")

# ─── all_active_candidates.csv ────────────────────────────────────────────────
all_path = ACTIVE_CSV / "all_active_candidates.csv"
with open(all_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=CANDIDATE_COLS)
    writer.writeheader()
    writer.writerows(all_rows_out)
print(f"[OK] all_active_candidates.csv: {len(all_rows_out)} rows")

# ─── top50_implementation_status.csv ─────────────────────────────────────────
top50_status_cols = [
    "original_rank", "crosswalk_id", "implemented", "net_group",
    "active_net_file", "active_candidate_csv", "recovery_type",
    "final_status", "note",
]

# Build lookup: cid -> net_group
cid_to_net = {}
for cid, ng in ALL_ACTIVE_IDS:
    cid_to_net[cid] = ng

top50_status_rows = []
for rank_idx, row in enumerate(top50_sorted):
    cid = row["crosswalk_id"]
    if cid in cid_to_net:
        ng = cid_to_net[cid]
        meta = NET_META[ng]
        top50_status_rows.append({
            "original_rank": rank_idx + 1,
            "crosswalk_id": cid,
            "implemented": "False",
            "net_group": ng,
            "active_net_file": meta["net_file"],
            "active_candidate_csv": meta["candidate_csv"],
            "recovery_type": meta["recovery_type"],
            "final_status": "CANDIDATE_TABLE_READY",
            "note": get_note(cid, ng),
        })
    else:
        top50_status_rows.append({
            "original_rank": rank_idx + 1,
            "crosswalk_id": cid,
            "implemented": "False",
            "net_group": "UNASSIGNED",
            "active_net_file": "",
            "active_candidate_csv": "",
            "recovery_type": "",
            "final_status": "NOT_IN_ANY_NET_GROUP",
            "note": "top50 candidate not assigned to any active net group",
        })

top50_path = ACTIVE_CSV / "top50_implementation_status.csv"
with open(top50_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=top50_status_cols)
    writer.writeheader()
    writer.writerows(top50_status_rows)
print(f"[OK] top50_implementation_status.csv: {len(top50_status_rows)} rows")

# ─── data/active top50 CSV (English cols, English values only) ───────────────
safe_cols = [
    "crosswalk_id", "lon", "lat", "lanes", "road_rank", "max_spd",
    "elderly_ratio", "accident_count", "crosswalk_length", "has_signal",
    "accident_count_50m", "accident_count_100m", "accident_count_200m",
    "night_accident_ratio", "link_count", "is_oneway", "time_gap_basic",
    "risk_score", "source_crosswalk_id", "canonical_crosswalk_id",
]
# Only keep columns that exist in the source
if top50_sorted:
    available_cols = list(top50_sorted[0].keys())
    safe_cols_filtered = [c for c in safe_cols if c in available_cols]

    active_top50_path = DATA_ACTIVE / "crosswalk_stepwise_result_50m_top50_active.csv"
    with open(active_top50_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["original_rank"] + safe_cols_filtered, extrasaction="ignore")
        writer.writeheader()
        for i, row in enumerate(top50_sorted):
            out_row = {"original_rank": i + 1}
            for col in safe_cols_filtered:
                out_row[col] = row.get(col, "")
            writer.writerow(out_row)
    print(f"[OK] data/active/crosswalk_stepwise_result_50m_top50_active.csv: {len(top50_sorted)} rows")

# ─── net_group_summary.csv ────────────────────────────────────────────────────
summary_cols = [
    "net_group", "net_file", "candidate_csv", "crosswalk_count",
    "baseline_runs_planned", "smart_runs_planned", "total_runs_planned",
    "recovery_type", "note",
]
summary_rows = [
    {
        "net_group": "current_main_12",
        "net_file": "result/active/nets/current_main_12.net.xml",
        "candidate_csv": "result/active/csv/current_main_12_candidates.csv",
        "crosswalk_count": 12,
        "baseline_runs_planned": 30,
        "smart_runs_planned": 360,
        "total_runs_planned": 390,
        "recovery_type": "batch_tls_recovery",
        "note": "primary main experiment",
    },
    {
        "net_group": "signal_fix_9",
        "net_file": "result/active/nets/signal_fix_9.net.xml",
        "candidate_csv": "result/active/csv/signal_fix_9_candidates.csv",
        "crosswalk_count": 9,
        "baseline_runs_planned": 30,
        "smart_runs_planned": 270,
        "total_runs_planned": 300,
        "recovery_type": "signal_fix_recovery",
        "note": "separate recovery experiment",
    },
    {
        "net_group": "generated_signal_7",
        "net_file": "result/active/nets/generated_signal_7.net.xml",
        "candidate_csv": "result/active/csv/generated_signal_7_candidates.csv",
        "crosswalk_count": 7,
        "baseline_runs_planned": 30,
        "smart_runs_planned": 210,
        "total_runs_planned": 240,
        "recovery_type": "generated_signal_expansion",
        "note": "separate expansion experiment",
    },
    {
        "net_group": "p1_p4_recovery_6",
        "net_file": "result/active/nets/p1_p4_recovery_6.net.xml",
        "candidate_csv": "result/active/csv/p1_p4_recovery_6_candidates.csv",
        "crosswalk_count": 6,
        "baseline_runs_planned": 30,
        "smart_runs_planned": 180,
        "total_runs_planned": 210,
        "recovery_type": "p1_signal_fix_plus_p4_approved",
        "note": "P1 5 + P4 approved 1 (NODE_5938 only; NODE_5830 dropped)",
    },
]
summary_path = ACTIVE_CSV / "net_group_summary.csv"
with open(summary_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=summary_cols)
    writer.writeheader()
    writer.writerows(summary_rows)
print(f"[OK] net_group_summary.csv: {len(summary_rows)} rows")

print("\n=== All active CSVs built successfully ===")
print(f"Active CSV dir: {ACTIVE_CSV}")
print(f"Data active dir: {DATA_ACTIVE}")
