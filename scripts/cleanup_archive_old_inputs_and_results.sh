#!/usr/bin/env bash
set -euo pipefail

BASE="/Users/junlee/Desktop/2026-1/js"
RESULT_ROOT="$BASE/result"
ACTIVE_ROOT="$RESULT_ROOT/active"
OLDRESULT_ROOT="$BASE/oldresult"
OLDDATA_ROOT="$BASE/olddata"
PLAN_ROOT="$ACTIVE_ROOT/cleanup_archive_plan"

APPLY=0
if [[ "${1:-}" == "--apply" ]]; then
  APPLY=1
  shift
fi

if [[ $# -gt 0 ]]; then
  echo "usage: $0 [--apply]" >&2
  exit 2
fi

mkdir -p "$OLDRESULT_ROOT" "$OLDDATA_ROOT" "$PLAN_ROOT"

python3 - "$BASE" "$APPLY" "$PLAN_ROOT" "$RESULT_ROOT" "$ACTIVE_ROOT" "$OLDRESULT_ROOT" "$OLDDATA_ROOT" <<'PY'
from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BASE = Path(sys.argv[1])
APPLY = sys.argv[2] == "1"
PLAN_ROOT = Path(sys.argv[3])
RESULT_ROOT = Path(sys.argv[4])
ACTIVE_ROOT = Path(sys.argv[5])
OLDRESULT_ROOT = Path(sys.argv[6])
OLDDATA_ROOT = Path(sys.argv[7])

TS = datetime.now().strftime("%Y%m%d_%H%M%S")
RESULT_ARCHIVE_ROOT = OLDRESULT_ROOT / f"archived_{TS}"
DATA_ARCHIVE_ROOT = OLDDATA_ROOT / f"archived_{TS}"

RESULT_ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
DATA_ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)

def run_git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(BASE), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout


tracked = {
    line.strip()
    for line in run_git("ls-files").splitlines()
    if line.strip()
}

def is_tracked(path: Path) -> bool:
    rel = path.relative_to(BASE).as_posix()
    if rel in tracked:
        return True
    prefix = rel.rstrip("/") + "/"
    return any(item.startswith(prefix) for item in tracked)


def unique_path(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem = dest.name
    parent = dest.parent
    suffix = ""
    if dest.is_file() or "." in stem:
      # file-like target
        p = dest
        i = 1
        while p.exists():
            if "." in stem:
                name = dest.stem + f"_dup{i}" + dest.suffix
            else:
                name = stem + f"_dup{i}"
            p = parent / name
            i += 1
        return p
    i = 1
    p = dest
    while p.exists():
        p = parent / f"{stem}_dup{i}"
        i += 1
    return p


def move_target(source: Path, archive_root: Path, preserve_source_path: bool) -> Path:
    if preserve_source_path:
        rel = source.relative_to(BASE)
        return unique_path(archive_root / rel)
    return unique_path(archive_root / source.name)


RESULT_KEEP = {
    "active",
    "old",
    "phase6_transition_after_recovery_20260514_220549",
    "phase_next_p1_p4_combined_recovery_20260516_020409",
    "phase_next_recovery_command_plan_20260514_200908",
    "phase_next_remaining22_p1_signal_fix_20260516_010953",
    "phase_next_top50_max_installation_recovery_20260516_002620",
    "phase_next_p1_p4_recovery_6_exclude_node5830_20260516_025057",
    "pipeline_smoke_2runs_p1p4_recovery_6_NODE_10060_s1",
}

RESULT_MOVE_PREFIXES = (
    "check_scoped_csv_layout",
    "cw_NODE_10376_smart",
    "node_10376_smart",
    "phase6_mini_experiment_batch03_",
    "phase6_smoke_",
    "pipeline_open_baseline_",
    "quick_reuse_",
    "reuse_",
    "phase_next_",
)

DATA_KEEP = {
    "data/T1_accident_crosswalk.csv",
    "data/T2_crosswalk_features.csv",
    "data/crosswalk_stepwise_result_50m.csv",
    "data/active/crosswalk_stepwise_result_50m_top50_active.csv",
    "data/processed/T1_accident_crosswalk.csv",
    "data/processed/T2_crosswalk_features.csv",
    "smart_crosswalk_sumo/data/T1_accident_crosswalk.csv",
    "smart_crosswalk_sumo/data/T2_crosswalk_features.csv",
    "smart_crosswalk_sumo/data/crosswalk_stepwise_result_50m.csv",
    "smart_crosswalk_sumo/data/junggu_admin_boundary.geojson",
    "data/raw/accidents_with_coords.csv",
    "data/raw/crosswalk_seoul.csv",
    "data/raw/elderly_pop_dong.csv",
    "data/raw/elderly_pop_gu.csv",
    "data/raw/ped_signal.csv",
    "data/raw/taas_raw.xlsx",
    "data/taas_calibration_targets.json",
}

DATA_MOVE = {
    "smart_crosswalk_sumo/data/T2_crosswalk_features_all.csv",
    "smart_crosswalk_sumo/data/T2_crosswalk_features_old.csv",
    "smart_crosswalk_sumo/data/crosswalk_stepwise_result.before_phase6_status_update_20260515_183920.csv",
    "smart_crosswalk_sumo/data/crosswalk_stepwise_result_50m.before_phase6_status_update_20260515_183920.csv",
    "smart_crosswalk_sumo/data/crosswalk_stepwise_result_50m.before_sumo_restored_columns.csv",
    "smart_crosswalk_sumo/data/crosswalk_stepwise_result_50m.before_top33_recovery.csv",
}

DATA_REVIEW = {
    "smart_crosswalk_sumo/data/crosswalk_stepwise_result.csv",
}

CURRENT_ACTIVE_OUTPUTS = {
    "result/active/commands",
    "result/active/csv",
    "result/active/logs",
    "result/active/manifests",
    "result/active/nets",
    "result/active/pedestrian_assumption",
    "result/active/readme",
    "result/active/real_30seed_runs",
    "result/active/validation",
    "result/active/vehicle_flow_audit",
}

ACTIVE_HISTORY = {
    "result/active/csv/status_backup_before_runnable_rebuild",
    "result/active/old_commands",
    "result/active/old_p1_p4_recovery_7",
    "result/active/old_real_30seed_runs",
}


@dataclass
class Row:
    scope: str
    kind: str
    source: str
    action: str
    reason: str
    tracked: bool
    source_exists: bool
    destination: str
    destination_exists: bool


rows_result: list[Row] = []
rows_data: list[Row] = []
review_rows: list[Row] = []

def add_row(bucket: list[Row], **kwargs: object) -> None:
    bucket.append(Row(**kwargs))


def classify_result_dir(path: Path) -> tuple[str, str]:
    name = path.name
    if name in RESULT_KEEP:
        return "keep", "current active or explicitly preserved"
    if any(name.startswith(prefix) for prefix in RESULT_MOVE_PREFIXES):
        return "move", "stale run result folder"
    return "review", "not clearly active; manual review required"


def classify_data_item(rel: str) -> tuple[str, str]:
    if rel in DATA_MOVE:
        return "move", "old backup or stale derived input"
    if rel in DATA_REVIEW:
        return "review", "ambiguous duplicate or historical input"
    if rel in DATA_KEEP:
        return "keep", "canonical or explicitly preserved input"
    return "review", "not in explicit keep/move list"


def active_keep_rows() -> list[Row]:
    out: list[Row] = []
    for rel in sorted(CURRENT_ACTIVE_OUTPUTS):
        p = BASE / rel
        add_row(
            out,
            scope="active",
            kind="current_active_output",
            source=rel,
            action="keep",
            reason="active working set",
            tracked=is_tracked(p),
            source_exists=p.exists(),
            destination="",
            destination_exists=False,
        )
    for rel in sorted(ACTIVE_HISTORY):
        p = BASE / rel
        add_row(
            out,
            scope="active",
            kind="active_history",
            source=rel,
            action="review",
            reason="historical artifact inside active; do not move in this pass",
            tracked=is_tracked(p),
            source_exists=p.exists(),
            destination="",
            destination_exists=False,
        )
    return out


def active_history_rows(rows: list[Row]) -> list[Row]:
    return [row for row in rows if row.action == "review"]


def plan_result_dirs() -> None:
    for path in sorted(RESULT_ROOT.iterdir()):
        if not path.is_dir():
            continue
        rel = path.relative_to(BASE).as_posix()
        if path.name in {"active", "old"}:
            add_row(
                rows_result,
                scope="result",
                kind="top_level_dir",
                source=rel,
                action="keep",
                reason="archive root or active root",
                tracked=is_tracked(path),
                source_exists=True,
                destination="",
                destination_exists=False,
            )
            continue
        action, reason = classify_result_dir(path)
        if action == "move" and is_tracked(path):
            action = "review"
            reason = "tracked result path excluded from move"
        dest = ""
        if action == "move":
            dest = str(move_target(path, RESULT_ARCHIVE_ROOT, preserve_source_path=False))
        add_row(
            rows_result,
            scope="result",
            kind="top_level_dir",
            source=rel,
            action=action,
            reason=reason,
            tracked=is_tracked(path),
            source_exists=True,
            destination=dest,
            destination_exists=Path(dest).exists() if dest else False,
        )


def plan_data_items() -> None:
    checked = [
        "data/T1_accident_crosswalk.csv",
        "data/T2_crosswalk_features.csv",
        "data/active/crosswalk_stepwise_result_50m_top50_active.csv",
        "data/crosswalk_stepwise_result_50m.csv",
        "data/taas_calibration_targets.json",
        "data/raw/accidents_with_coords.csv",
        "data/raw/crosswalk_seoul.csv",
        "data/raw/elderly_pop_dong.csv",
        "data/raw/elderly_pop_gu.csv",
        "data/raw/ped_signal.csv",
        "data/raw/taas_raw.xlsx",
        "data/processed/T1_accident_crosswalk.csv",
        "data/processed/T2_crosswalk_features.csv",
        "smart_crosswalk_sumo/data/T1_accident_crosswalk.csv",
        "smart_crosswalk_sumo/data/T2_crosswalk_features.csv",
        "smart_crosswalk_sumo/data/T2_crosswalk_features_all.csv",
        "smart_crosswalk_sumo/data/T2_crosswalk_features_old.csv",
        "smart_crosswalk_sumo/data/crosswalk_stepwise_result.before_phase6_status_update_20260515_183920.csv",
        "smart_crosswalk_sumo/data/crosswalk_stepwise_result.csv",
        "smart_crosswalk_sumo/data/crosswalk_stepwise_result_50m.before_phase6_status_update_20260515_183920.csv",
        "smart_crosswalk_sumo/data/crosswalk_stepwise_result_50m.before_sumo_restored_columns.csv",
        "smart_crosswalk_sumo/data/crosswalk_stepwise_result_50m.before_top33_recovery.csv",
        "smart_crosswalk_sumo/data/crosswalk_stepwise_result_50m.csv",
        "smart_crosswalk_sumo/data/junggu_admin_boundary.geojson",
    ]
    for rel in checked:
        p = BASE / rel
        if not p.exists():
            continue
        action, reason = classify_data_item(rel)
        if action == "move" and is_tracked(p):
            action = "review"
            reason = "tracked data path excluded from move"
        dest = ""
        if action == "move":
            dest = str(move_target(p, DATA_ARCHIVE_ROOT, preserve_source_path=True))
        row = Row(
            scope="data",
            kind="input_or_derived",
            source=rel,
            action=action,
            reason=reason,
            tracked=is_tracked(p),
            source_exists=True,
            destination=dest,
            destination_exists=Path(dest).exists() if dest else False,
        )
        rows_data.append(row)
        if action == "review":
            review_rows.append(row)


def write_csv(path: Path, rows: list[Row]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "scope",
                "kind",
                "source",
                "action",
                "reason",
                "tracked",
                "source_exists",
                "destination",
                "destination_exists",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "scope": row.scope,
                    "kind": row.kind,
                    "source": row.source,
                    "action": row.action,
                    "reason": row.reason,
                    "tracked": str(row.tracked).lower(),
                    "source_exists": str(row.source_exists).lower(),
                    "destination": row.destination,
                    "destination_exists": str(row.destination_exists).lower(),
                }
            )


def print_rows(title: str, rows: list[Row]) -> None:
    print(f"\n## {title}")
    for row in rows:
        dest = f" -> {row.destination}" if row.destination else ""
        tracked = "tracked" if row.tracked else "untracked"
        print(f"{row.action.upper():5} {row.source}{dest} [{tracked}] {row.reason}")


def move_one(src: Path, dest: Path) -> tuple[bool, str]:
    if not src.exists():
        return False, "missing source"
    final_dest = unique_path(dest)
    final_dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(final_dest))
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return True, str(final_dest)


def unique_path(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = "".join(dest.suffixes)
    if dest.is_dir() or not suffix:
        i = 1
        while True:
            candidate = dest.parent / f"{dest.name}_dup{i}"
            if not candidate.exists():
                return candidate
            i += 1
    i = 1
    while True:
        candidate = dest.parent / f"{stem}_dup{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def execute_moves(rows: list[Row]) -> tuple[int, int]:
    moved = 0
    failed = 0
    for row in rows:
        if row.action != "move" or not row.destination:
            continue
        src = BASE / row.source
        dest = Path(row.destination)
        ok, info = move_one(src, dest)
        if ok:
            moved += 1
            print(f"MOVED {row.source} -> {info}")
        else:
            failed += 1
            print(f"FAIL  {row.source} :: {info}")
    return moved, failed


plan_result_dirs()
plan_data_items()
active_rows = active_keep_rows()
active_review_rows = active_history_rows(active_rows)

result_move_rows = [r for r in rows_result if r.action == "move"]
result_keep_rows = [r for r in rows_result if r.action == "keep"]
result_review_rows = [r for r in rows_result if r.action == "review"]
data_move_rows = [r for r in rows_data if r.action == "move"]
data_keep_rows = [r for r in rows_data if r.action == "keep"]
data_review_rows = [r for r in rows_data if r.action == "review"]

write_csv(PLAN_ROOT / "move_plan_result.csv", rows_result + active_rows)
write_csv(PLAN_ROOT / "move_plan_data.csv", rows_data)
write_csv(PLAN_ROOT / "keep_review_required.csv", result_review_rows + data_review_rows + active_review_rows)

report = PLAN_ROOT / "cleanup_archive_report.md"
with report.open("w", encoding="utf-8") as f:
    f.write("# Cleanup Archive Report\n\n")
    f.write(f"- timestamp: {TS}\n")
    f.write(f"- mode: {'apply' if APPLY else 'dry-run'}\n")
    f.write(f"- result move candidates: {len(result_move_rows)}\n")
    f.write(f"- result keep targets: {len(result_keep_rows)}\n")
    f.write(f"- result keep_review_required: {len(result_review_rows) + len(active_review_rows)}\n")
    f.write(f"- data move candidates: {len(data_move_rows)}\n")
    f.write(f"- data keep targets: {len(data_keep_rows)}\n")
    f.write(f"- data keep_review_required: {len(data_review_rows)}\n")
    f.write(f"- active current outputs kept: {len([r for r in active_rows if r.action == 'keep'])}\n")
    f.write(f"- active history review items: {len(active_review_rows)}\n")
    f.write(f"- tracked move candidates: {sum(1 for r in result_move_rows + data_move_rows if r.tracked)}\n")
    f.write("\n## Active keep split\n\n")
    f.write("- current active outputs: `result/active/commands`, `result/active/csv`, `result/active/logs`, `result/active/manifests`, `result/active/nets`, `result/active/pedestrian_assumption`, `result/active/readme`, `result/active/real_30seed_runs`, `result/active/validation`, `result/active/vehicle_flow_audit`\n")
    f.write("- active history: `result/active/csv/status_backup_before_runnable_rebuild`, `result/active/old_commands`, `result/active/old_p1_p4_recovery_7`, `result/active/old_real_30seed_runs`\n")
    f.write("\n## Preserved canonical inputs\n\n")
    f.write("- `smart_crosswalk_sumo/data/T1_accident_crosswalk.csv`\n")
    f.write("- `smart_crosswalk_sumo/data/T2_crosswalk_features.csv`\n")
    f.write("- `smart_crosswalk_sumo/data/crosswalk_stepwise_result_50m.csv`\n")
    f.write("- `smart_crosswalk_sumo/data/junggu_admin_boundary.geojson`\n")
    f.write("- `data/processed/T1_accident_crosswalk.csv`\n")
    f.write("- `data/processed/T2_crosswalk_features.csv`\n")
    f.write("- `data/crosswalk_stepwise_result_50m.csv`\n")
    f.write("\n## Review required\n\n")
    for row in result_review_rows + data_review_rows + active_review_rows:
        f.write(f"- `{row.source}` ({row.reason})\n")
    f.write("\n## Plan files\n\n")
    f.write("- `result/active/cleanup_archive_plan/move_plan_result.csv`\n")
    f.write("- `result/active/cleanup_archive_plan/move_plan_data.csv`\n")
    f.write("- `result/active/cleanup_archive_plan/keep_review_required.csv`\n")

print("git status --short")
print(run_git("status", "--short").rstrip() or "(clean)")
print(f"\nDry-run plan written to: {PLAN_ROOT}")
print_rows("Result plan", rows_result + active_rows)
print_rows("Data plan", rows_data)
print(f"\nPlanned result move count: {len(result_move_rows)}")
print(f"Planned result keep count: {len(result_keep_rows)}")
print(f"Planned keep_review_required count: {len(result_review_rows) + len(data_review_rows) + len(active_review_rows)}")
print(f"Planned data move count: {len(data_move_rows)}")
print(f"Planned data keep count: {len(data_keep_rows)}")
print(f"Tracked move candidates: {sum(1 for r in result_move_rows + data_move_rows if r.tracked)}")

if APPLY:
    moved, failed = execute_moves(result_move_rows + data_move_rows)
    print(f"\nMoved files/dirs: {moved}")
    print(f"Failed moves: {failed}")
    print("\ngit status --short")
    print(run_git("status", "--short").rstrip() or "(clean)")
PY
