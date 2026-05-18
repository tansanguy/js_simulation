#!/usr/bin/env python3
"""
build_inventory_and_cleanup.py
Creates artifact_inventory.csv with KEEP/ACTIVE_COPY/MOVE_TO_OLD/DO_NOT_TOUCH
and cleanup_dry_run.md.
Does NOT delete or move anything — dry-run only.
"""

import csv
import os
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
RESULT = BASE / "result"
DATA = BASE / "data"
ACTIVE_CSV = RESULT / "active/csv"
ACTIVE_README = RESULT / "active/readme"
ACTIVE_README.mkdir(parents=True, exist_ok=True)

# Active net source dirs — DO NOT TOUCH
PROTECTED_RESULT_DIRS = {
    "phase_next_recovery_command_plan_20260514_200908",
    "phase_next_top50_max_installation_recovery_20260516_002620",
    "phase_next_p1_p4_combined_recovery_20260516_020409",
    "active",
}

# Pattern-based MOVE_TO_OLD candidates (result/)
MOVE_PATTERNS = [
    "quick_reuse",
    "smoke",
    "debug",
    "temporary",
    "pipeline_open_baseline",
    "reuse_",
    "check_scoped_csv_layout",
    "cw_NODE_10376_smart",
    "node_10376_smart",
]

# Items to keep (significant history)
KEEP_PATTERNS = [
    "phase_next_recovery_command_plan_20260514_200908",
    "phase_next_top50_max_installation_recovery_20260516_002620",
    "phase_next_p1_p4_combined_recovery_20260516_020409",
    "phase_next_p1_p4_recovery_experiment_plan_20260516_013616",
    "phase_next_p1_p4_recovery_experiment_plan_20260516_013716",
    "phase_next_remaining22_p1_signal_fix_20260516_010953",
    "phase_next_top50_recovery_integrity_audit_20260516_012731",
    "phase_next_top50_ready33_status_update_20260516_011912",
    "active",  # new active dir
]

inv_rows = []
move_candidates = []
keep_list = []

# --- Inventory result/ subdirs ---
for item in sorted(RESULT.iterdir()):
    if item.name.startswith("."):
        continue
    if item.is_file():
        row = {
            "path": str(item.relative_to(BASE)),
            "type": "file",
            "size_bytes": item.stat().st_size,
            "classification": "KEEP",
            "reason": "loose result file - keep",
        }
        inv_rows.append(row)
        continue

    name = item.name
    cls = "KEEP"
    reason = ""

    if name in PROTECTED_RESULT_DIRS:
        cls = "DO_NOT_TOUCH"
        reason = "active net source or active dir"
    elif any(name.startswith(p) for p in MOVE_PATTERNS):
        cls = "MOVE_TO_OLD"
        reason = "matches move pattern (quick_reuse/smoke/debug/pipeline_open/reuse)"
        move_candidates.append(str(item.relative_to(BASE)))
    elif name in KEEP_PATTERNS:
        cls = "KEEP"
        reason = "significant history or reference"
    else:
        # Older phase_next dirs — candidate for MOVE_TO_OLD
        if "phase_next" in name:
            cls = "MOVE_TO_OLD"
            reason = "older phase_next result, not in active source list"
            move_candidates.append(str(item.relative_to(BASE)))
        else:
            cls = "KEEP"
            reason = "no pattern match - keep by default"

    # Count items
    try:
        child_count = sum(1 for _ in item.rglob("*"))
    except Exception:
        child_count = 0

    row = {
        "path": str(item.relative_to(BASE)),
        "type": "dir",
        "size_bytes": child_count,
        "classification": cls,
        "reason": reason,
    }
    inv_rows.append(row)
    if cls == "KEEP":
        keep_list.append(str(item.relative_to(BASE)))

# --- Inventory data/ ---
data_old_candidates = []
for item in sorted(DATA.iterdir()):
    if item.name.startswith("."):
        continue
    if item.is_file() and item.name == "crosswalk_stepwise_result_50m.csv":
        inv_rows.append({
            "path": str(item.relative_to(BASE)),
            "type": "file",
            "size_bytes": item.stat().st_size,
            "classification": "DO_NOT_TOUCH",
            "reason": "primary base CSV - must never be moved",
        })
    elif item.is_file() and item.name == "taas_calibration_targets.json":
        inv_rows.append({
            "path": str(item.relative_to(BASE)),
            "type": "file",
            "size_bytes": item.stat().st_size,
            "classification": "KEEP",
            "reason": "calibration target data",
        })
    elif item.is_dir() and item.name in ("processed", "raw", "active", "old"):
        inv_rows.append({
            "path": str(item.relative_to(BASE)),
            "type": "dir",
            "size_bytes": sum(1 for _ in item.rglob("*")),
            "classification": "KEEP",
            "reason": "data subdirectory - keep",
        })
    elif item.is_file():
        inv_rows.append({
            "path": str(item.relative_to(BASE)),
            "type": "file",
            "size_bytes": item.stat().st_size,
            "classification": "MOVE_TO_OLD",
            "reason": "derived/intermediary data file - move to data/old",
        })
        data_old_candidates.append(str(item.relative_to(BASE)))
    else:
        inv_rows.append({
            "path": str(item.relative_to(BASE)),
            "type": "dir",
            "size_bytes": 0,
            "classification": "KEEP",
            "reason": "unclassified dir - keep by default",
        })

# Write inventory CSV
inv_cols = ["path", "type", "size_bytes", "classification", "reason"]
inv_path = ACTIVE_CSV / "artifact_inventory.csv"
with open(inv_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=inv_cols)
    writer.writeheader()
    writer.writerows(inv_rows)
print(f"[OK] artifact_inventory.csv: {len(inv_rows)} rows")

# ─── Write cleanup_dry_run.md ─────────────────────────────────────────────────
dry_run_path = ACTIVE_README / "cleanup_dry_run.md"
with open(dry_run_path, "w", encoding="utf-8") as f:
    f.write("# Cleanup Dry-Run Plan\n\n")
    f.write("> **DRY-RUN ONLY** — No files have been moved or deleted.\n")
    f.write("> Review this file before running the actual cleanup.\n\n")

    f.write("## DO_NOT_TOUCH (Active source dirs)\n\n")
    for name in PROTECTED_RESULT_DIRS:
        f.write(f"- `result/{name}/`\n")
    f.write("\n")

    f.write("## result/ → result/old/ (Move candidates)\n\n")
    for p in sorted(set(move_candidates)):
        f.write(f"- `{p}`\n")
    f.write("\n")

    f.write("## data/ → data/old/ (Derived file move candidates)\n\n")
    for p in sorted(set(data_old_candidates)):
        f.write(f"- `{p}`\n")
    f.write("\n")

    f.write("## KEEP in result/ (Reference history)\n\n")
    for p in sorted(keep_list):
        f.write(f"- `{p}`\n")
    f.write("\n")

    f.write("## Execution command (after review)\n\n")
    f.write("```bash\n")
    f.write("# Execute actual moves:\n")
    f.write("python3 scripts/execute_cleanup.py\n")
    f.write("```\n")

print(f"[OK] cleanup_dry_run.md written")
print(f"  result/ MOVE_TO_OLD: {len(set(move_candidates))} dirs")
print(f"  data/ MOVE_TO_OLD: {len(set(data_old_candidates))} files")
