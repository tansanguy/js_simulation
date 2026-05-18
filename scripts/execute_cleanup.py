#!/usr/bin/env python3
"""
execute_cleanup.py
Reads the dry-run plan and actually moves result/ dirs to result/old/
and data/ derived files to data/old/.
NO deletion. NO git operations.
Run ONLY after reviewing cleanup_dry_run.md.
"""

import shutil
from pathlib import Path
import csv
from datetime import datetime

BASE = Path(__file__).resolve().parents[1]
RESULT = BASE / "result"
DATA = BASE / "data"
RESULT_OLD = RESULT / "old"
DATA_OLD = DATA / "old"
RESULT_OLD.mkdir(parents=True, exist_ok=True)
DATA_OLD.mkdir(parents=True, exist_ok=True)

ACTIVE_CSV = RESULT / "active/csv"
ACTIVE_README = RESULT / "active/readme"

# Protected: never move
PROTECTED_RESULT_DIRS = {
    "phase_next_recovery_command_plan_20260514_200908",
    "phase_next_top50_max_installation_recovery_20260516_002620",
    "phase_next_p1_p4_combined_recovery_20260516_020409",
    "active",
    "old",
}

KEEP_RESULT_DIRS = {
    "phase_next_p1_p4_recovery_experiment_plan_20260516_013616",
    "phase_next_p1_p4_recovery_experiment_plan_20260516_013716",
    "phase_next_remaining22_p1_signal_fix_20260516_010953",
    "phase_next_top50_recovery_integrity_audit_20260516_012731",
    "phase_next_top50_ready33_status_update_20260516_011912",
}

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

# Protected data files
PROTECTED_DATA_FILES = {
    "crosswalk_stepwise_result_50m.csv",
    "taas_calibration_targets.json",
    ".DS_Store",
}

exec_log = []

def log(action, src, dst, status, note=""):
    exec_log.append({
        "timestamp": datetime.now().isoformat(),
        "action": action,
        "source": str(src),
        "destination": str(dst),
        "status": status,
        "note": note,
    })
    print(f"[{status}] {action}: {src} -> {dst}  {note}")


def safe_move(src, dst_dir):
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.exists():
        # Rename with suffix
        dst = dst_dir / (src.name + "_dup")
    shutil.move(str(src), str(dst))
    return dst


# --- Move result/ dirs to result/old/ ---
moved_result = 0
for item in sorted(RESULT.iterdir()):
    if item.name.startswith(".") or item.is_file():
        continue
    name = item.name
    if name in PROTECTED_RESULT_DIRS or name in KEEP_RESULT_DIRS:
        log("SKIP", item.relative_to(BASE), "", "PROTECTED_OR_KEEP")
        continue
    if any(name.startswith(p) for p in MOVE_PATTERNS):
        try:
            dst = safe_move(item, RESULT_OLD)
            log("MOVE", item.relative_to(BASE), dst, "OK", "matches move pattern")
            moved_result += 1
        except Exception as e:
            log("MOVE", item.relative_to(BASE), RESULT_OLD, "ERROR", str(e))
    elif "phase_next" in name:
        try:
            dst = safe_move(item, RESULT_OLD)
            log("MOVE", item.relative_to(BASE), dst, "OK", "older phase_next")
            moved_result += 1
        except Exception as e:
            log("MOVE", item.relative_to(BASE), RESULT_OLD, "ERROR", str(e))
    else:
        log("SKIP", item.relative_to(BASE), "", "NO_PATTERN_MATCH_KEEP")

# --- Move data/ derived files to data/old/ ---
moved_data = 0
for item in sorted(DATA.iterdir()):
    if item.name.startswith(".") or not item.is_file():
        continue
    if item.name in PROTECTED_DATA_FILES:
        log("SKIP", item.relative_to(BASE), "", "PROTECTED_DATA_FILE")
        continue
    try:
        dst = safe_move(item, DATA_OLD)
        log("MOVE", item.relative_to(BASE), dst, "OK", "derived data file")
        moved_data += 1
    except Exception as e:
        log("MOVE", item.relative_to(BASE), DATA_OLD, "ERROR", str(e))

# --- Write execution summary ---
summary_path = ACTIVE_README / "cleanup_execution_summary.md"
with open(summary_path, "w", encoding="utf-8") as f:
    f.write("# Cleanup Execution Summary\n\n")
    f.write(f"- Run at: {datetime.now().isoformat()}\n")
    f.write(f"- result/ dirs moved to result/old/: {moved_result}\n")
    f.write(f"- data/ files moved to data/old/: {moved_data}\n\n")
    f.write("## Log\n\n")
    f.write("| action | source | destination | status | note |\n")
    f.write("|--------|--------|-------------|--------|------|\n")
    for row in exec_log:
        f.write(f"| {row['action']} | {row['source']} | {row['destination']} | {row['status']} | {row['note']} |\n")

# Write CSV log
log_csv = ACTIVE_CSV / "cleanup_execution_log.csv"
with open(log_csv, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=["timestamp","action","source","destination","status","note"])
    writer.writeheader()
    writer.writerows(exec_log)

print(f"\n=== Cleanup done ===")
print(f"  result/ moved: {moved_result}")
print(f"  data/ moved:   {moved_data}")
print(f"  Summary: {summary_path}")
