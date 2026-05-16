#!/usr/bin/env python3
"""
smart_crosswalk_sumo/reporting/run_runtime_benchmark.py

Manifest-driven runtime benchmark runner.
Reads run_manifest.csv, selects 1 baseline + 1 smart per run_group (8 total),
executes each with run_phase6_recovery_smoke, and writes runtime_benchmark_summary.csv.

Usage:
    python3 -m smart_crosswalk_sumo.reporting.run_runtime_benchmark \\
        --manifest result/active/real_30seed_runs/run_manifest.csv \\
        --output  result/active/real_30seed_runs/benchmark/runtime_benchmark_summary.csv

Forbidden:
    - Full 30-seed execution
    - main.py --simulation_mode integrated_selected
    - Hardcoded candidate lists
    - Bash associative arrays
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REQUIRED_GROUPS = {
    "current_main_12",
    "signal_fix_9",
    "generated_signal_7",
    "p1_p4_recovery_6",
}
BLOCKER_IDS = {"NODE_5830"}
FORBIDDEN_PATTERN = "integrated_selected"

OUTPUT_COLS = [
    "run_group",
    "scenario",
    "crosswalk_id",
    "seed",
    "command",
    "output_dir",
    "log_file",
    "log_parent_exists",
    "log_exists",
    "start_time",
    "end_time",
    "elapsed_sec",
    "exit_code",
    "status",
    "note",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_manifest(manifest_path: Path) -> list[dict]:
    if not manifest_path.exists():
        sys.exit(f"[ERROR] Manifest not found: {manifest_path}")
    with manifest_path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit(f"[ERROR] Manifest is empty: {manifest_path}")
    return rows


def _validate_manifest(rows: list[dict]) -> None:
    total = len(rows)
    print(f"[INFO] Manifest rows: {total}")

    # Check NODE_5830
    blocker_rows = [r for r in rows if r.get("crosswalk_id", "") in BLOCKER_IDS]
    if blocker_rows:
        ids = [r["crosswalk_id"] for r in blocker_rows]
        sys.exit(f"[BLOCKER] Forbidden crosswalk IDs found in manifest: {ids}. Aborting.")
    print("[OK] NODE_5830 absent from manifest.")

    # Check groups
    groups = {r.get("run_group", "") for r in rows}
    missing = REQUIRED_GROUPS - groups
    if missing:
        sys.exit(f"[BLOCKER] Required run_groups missing from manifest: {missing}")
    extra = groups - REQUIRED_GROUPS
    if extra:
        print(f"[WARN] Extra run_groups in manifest (ignored): {extra}")
    print(f"[OK] Groups found: {sorted(groups & REQUIRED_GROUPS)}")


def _select_benchmark_rows(rows: list[dict]) -> list[dict]:
    """Select exactly 1 baseline seed=1 + 1 smart seed=1 per run_group (8 total)."""
    selected: list[dict] = []
    for group in sorted(REQUIRED_GROUPS):
        for scenario in ("baseline", "smart"):
            candidates = [
                r for r in rows
                if r.get("run_group") == group
                and r.get("scenario") == scenario
                and str(r.get("seed", "")).strip() == "1"
            ]
            if not candidates:
                sys.exit(
                    f"[BLOCKER] No seed=1 {scenario} row found for run_group={group}."
                )
            row = candidates[0]
            # Safety check: no blocker IDs in selected row
            cid = row.get("crosswalk_id", "")
            if cid in BLOCKER_IDS:
                sys.exit(f"[BLOCKER] Selected row has forbidden crosswalk_id={cid}")
            selected.append(row)

    assert len(selected) == 8, f"Expected 8 benchmark rows, got {len(selected)}"
    return selected


def _build_command(row: dict) -> str:
    """Use 'command' column if present, else build from individual columns."""
    cmd = row.get("command", "").strip()
    if cmd:
        if FORBIDDEN_PATTERN in cmd:
            sys.exit(
                f"[BLOCKER] Manifest command contains forbidden pattern "
                f"'{FORBIDDEN_PATTERN}': {cmd[:120]}"
            )
        return cmd

    # Build command from columns
    candidate_csv = row.get("candidate_csv", "").strip()
    net_file = row.get("net_file", "").strip()
    scenario = row.get("scenario", "").strip()
    seed = row.get("seed", "1").strip()
    output_dir = row.get("output_dir", "").strip()
    extension_sec = "0" if scenario == "baseline" else "5.0"

    if not candidate_csv or not net_file or not output_dir:
        sys.exit(
            f"[BLOCKER] Cannot build command: missing candidate_csv/net_file/output_dir "
            f"for run_group={row.get('run_group')} scenario={scenario} seed={seed}"
        )

    parts = [
        "python3", "-m", "smart_crosswalk_sumo.run_phase6_recovery_smoke",
        "--candidate-csv", candidate_csv,
        "--net-file", net_file,
        "--scenario", scenario,
        "--seed", seed,
        "--sim-duration", "600",
        "--warmup", "0",
        "--step-length", "0.5",
        "--extension-sec", extension_sec,
        "--phase-aligned-ped-depart",
        "--ped-repeat-count", "5",
        "--ped-repeat-spacing-sec", "2",
        "--output-dir", output_dir,
    ]
    return " ".join(shlex.quote(p) for p in parts)


def _parse_log_from_cmd(cmd: str) -> str | None:
    """Extract log file path from shell redirection in command string."""
    # Look for >"path" or >path
    match = re.search(r'>\s*"?([^">\s]+)"?', cmd)
    if match:
        return match.group(1)
    return None


def _run_one(row: dict, cmd: str, writer: csv.DictWriter) -> int:
    run_group = row.get("run_group", "")
    scenario = row.get("scenario", "")
    crosswalk_id = row.get("crosswalk_id", "")
    seed = row.get("seed", "")
    output_dir = row.get("output_dir", "")

    # Derive log_file
    log_file_str = row.get("log_file", "").strip()
    if not log_file_str:
        log_file_str = _parse_log_from_cmd(cmd) or ""

    log_path = Path(log_file_str) if log_file_str else None
    log_parent_exists = False
    if log_path:
        if log_path.parent.exists():
            log_parent_exists = True
        log_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"  [RUN] {run_group}/{scenario}/{crosswalk_id}/seed{seed}")

    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)

    start_time = _now_iso()
    t_start = datetime.now(timezone.utc)
    exit_code = -1
    status = "error"
    note = ""

    try:
        # Use shell redirection if present in cmd string
        has_redirection = ">" in cmd

        if has_redirection:
            result = subprocess.run(
                cmd,
                shell=True,
                check=False,
            )
        else:
            # Fallback for commands without redirection
            actual_log = log_path or (Path(output_dir) / "benchmark_run.log")
            actual_log.parent.mkdir(parents=True, exist_ok=True)
            with actual_log.open("w", encoding="utf-8") as log_f:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
        exit_code = result.returncode
        status = "ok" if exit_code == 0 else "failed"
        if exit_code != 0:
            note = f"non-zero exit: {exit_code}"
    except Exception as exc:
        note = str(exc)
        status = "error"

    elapsed = (datetime.now(timezone.utc) - t_start).total_seconds()
    end_time = _now_iso()
    log_exists = log_path.exists() if log_path else False

    out_row = {
        "run_group": run_group,
        "scenario": scenario,
        "crosswalk_id": crosswalk_id,
        "seed": seed,
        "command": cmd,
        "output_dir": output_dir,
        "log_file": str(log_path) if log_path else "",
        "log_parent_exists": log_parent_exists,
        "log_exists": log_exists,
        "start_time": start_time,
        "end_time": end_time,
        "elapsed_sec": f"{elapsed:.1f}",
        "exit_code": exit_code,
        "status": status,
        "note": note,
    }
    writer.writerow(out_row)
    print(f"    elapsed={elapsed:.1f}s  exit={exit_code}  status={status}  log_exists={log_exists}")
    return exit_code


def _estimate_full_runtime(summary_rows: list[dict]) -> None:
    """Print 30-seed full runtime estimate from benchmark results."""
    crosswalk_counts = {
        "current_main_12": 12,
        "signal_fix_9": 9,
        "generated_signal_7": 7,
        "p1_p4_recovery_6": 6,
    }
    SEEDS = 30

    from collections import defaultdict
    by_gs: dict[tuple, list[float]] = defaultdict(list)
    for r in summary_rows:
        try:
            by_gs[(r["run_group"], r["scenario"])].append(float(r["elapsed_sec"]))
        except (ValueError, KeyError):
            pass

    print("\n--- 30-seed full runtime estimate ---")
    print(f"{'run_group':<25} {'baseline_1run(s)':>17} {'smart_1run(s)':>14} {'est_total(h)':>13}")
    print("-" * 72)
    total_sec = 0.0
    for group, n_cw in sorted(crosswalk_counts.items()):
        b = by_gs.get((group, "baseline"), [0.0])
        s = by_gs.get((group, "smart"), [0.0])
        b_avg = sum(b) / len(b) if b else 0.0
        s_avg = sum(s) / len(s) if s else 0.0
        est = b_avg * SEEDS + s_avg * SEEDS * n_cw
        total_sec += est
        print(f"  {group:<23} {b_avg:>17.1f} {s_avg:>14.1f} {est/3600:>13.2f}")
    print("-" * 72)
    print(f"  {'TOTAL':<23} {'':>17} {'':>14} {total_sec/3600:>13.2f} h")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manifest-driven runtime benchmark: 1 baseline + 1 smart per group (8 runs)."
    )
    parser.add_argument(
        "--manifest",
        default="result/active/real_30seed_runs/run_manifest.csv",
        help="Path to run_manifest.csv (source of truth).",
    )
    parser.add_argument(
        "--output",
        default="result/active/real_30seed_runs/benchmark/runtime_benchmark_summary.csv",
        help="Path to write runtime_benchmark_summary.csv.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Select and print benchmark rows without running SUMO.",
    )
    args = parser.parse_args()

    manifest_path = Path(args.manifest).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()

    print(f"[INFO] Manifest: {manifest_path}")
    print(f"[INFO] Output:   {output_path}")

    rows = _load_manifest(manifest_path)
    _validate_manifest(rows)

    selected = _select_benchmark_rows(rows)

    print(f"\n[INFO] Selected {len(selected)} benchmark rows:")
    for r in selected:
        print(f"  {r.get('run_group'):<25} {r.get('scenario'):<10} "
              f"seed={r.get('seed')}  cid={r.get('crosswalk_id')}")

    if args.dry_run:
        print("\n[DRY-RUN] Stopping before execution (--dry-run).")
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)

    summary_rows: list[dict] = []
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLS)
        writer.writeheader()

        print(f"\n[INFO] Running {len(selected)} benchmark runs ...\n")
        for row in selected:
            cmd = _build_command(row)
            _run_one(row, cmd, writer)
            f.flush()

    # Re-read for estimate
    with output_path.open(encoding="utf-8") as f:
        summary_rows = list(csv.DictReader(f))

    _estimate_full_runtime(summary_rows)
    print(f"\n[DONE] Summary written to: {output_path}")


if __name__ == "__main__":
    main()
