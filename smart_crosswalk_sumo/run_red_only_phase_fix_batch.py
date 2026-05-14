#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pandas as pd

from smart_crosswalk_sumo.network_utils import apply_sumo_environment
from smart_crosswalk_sumo.run_tierwise_phase6_continue import (
    BASE_NET,
    FALLBACK_BASE_NET,
    _build_phase_fix_tllogic_patch,
    _collect_traci_controlled_links,
    _load_csv,
    _run_sumo_load_test,
    _validate_phase_fix,
)


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run red-only phase fix batch from prepared CSV.")
    parser.add_argument("--input-csv", required=True, help="Prepared red-only input CSV.")
    parser.add_argument("--output-dir", required=True, help="Run output directory.")
    parser.add_argument(
        "--previous-ready",
        required=True,
        help="Official cumulative phase6_ready CSV baseline.",
    )
    parser.add_argument(
        "--base-net",
        default="",
        help="Base network XML for phase fix; use batch-specific recovery_tls_batch_network_v1.net.xml.",
    )
    args = parser.parse_args()

    apply_sumo_environment()

    input_csv = Path(args.input_csv).expanduser().resolve()
    previous_ready = Path(args.previous_ready).expanduser().resolve()
    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    red_only = pd.read_csv(input_csv)
    if red_only.empty:
        raise ValueError(f"empty red-only csv: {input_csv}")
    if "recovery_outcome" in red_only.columns:
        bad = red_only[red_only["recovery_outcome"] != "RED_ONLY_PHASE_FIX_NEEDED"]
        if not bad.empty:
            raise ValueError("input csv contains non RED_ONLY_PHASE_FIX_NEEDED rows")

    prev_confirmed = _load_csv(previous_ready) if previous_ready.exists() else pd.DataFrame()
    if not prev_confirmed.empty and "promotion_source" not in prev_confirmed.columns:
        prev_confirmed = prev_confirmed.copy()
        prev_confirmed["promotion_source"] = "PREVIOUS"

    if args.base_net:
        base_net_file = Path(args.base_net).expanduser().resolve()
    else:
        base_net_file = BASE_NET if BASE_NET.exists() else FALLBACK_BASE_NET
    if not base_net_file.exists():
        raise FileNotFoundError(base_net_file)

    phase_fix_patch_path, _ = _build_phase_fix_tllogic_patch(base_net_file, red_only, out_dir)
    phase_fix_net_file = out_dir / "red_only_phase_fix_network_v1.net.xml"
    phase_fix_netconvert_cmd = [
        "netconvert",
        "--sumo-net-file",
        str(base_net_file),
        "--tllogic-files",
        str(phase_fix_patch_path),
        "--output-file",
        str(phase_fix_net_file),
        "--no-turnarounds",
        "true",
        "--walkingareas",
        "true",
        "--crossings.guess",
        "true",
        "--sidewalks.guess",
        "true",
        "--sidewalks.guess.min-speed",
        "0",
        "--sidewalks.guess.max-speed",
        "40",
        "--tls.crossing-min.time",
        "6",
        "--tls.crossing-clearance.time",
        "3",
    ]
    (out_dir / "phase_fix_netconvert_command.sh").write_text(
        "#!/bin/bash\n" + " ".join(phase_fix_netconvert_cmd) + "\n",
        encoding="utf-8",
    )
    phase_fix_ret = subprocess.run(phase_fix_netconvert_cmd, capture_output=True, text=True, timeout=1200)
    (out_dir / "phase_fix_netconvert.log").write_text(
        (phase_fix_ret.stdout or "") + "\n" + (phase_fix_ret.stderr or ""),
        encoding="utf-8",
    )
    if phase_fix_ret.returncode != 0 or not phase_fix_net_file.exists():
        raise RuntimeError(f"phase fix netconvert failed; see {out_dir / 'phase_fix_netconvert.log'}")

    phase_fix_load_ok, phase_fix_load_err = _run_sumo_load_test(phase_fix_net_file, end_time=5)
    phase_fix_root = ET.parse(phase_fix_net_file).getroot()
    phase_fix_traci_ids = {
        str(t.attrib.get("id", "") or "")
        for t in phase_fix_root.findall("tlLogic")
        if str(t.attrib.get("id", "") or "")
    }
    phase_fix_traci_ok, phase_fix_traci_err, phase_fix_traci_map = _collect_traci_controlled_links(
        phase_fix_net_file,
        phase_fix_traci_ids,
    )
    phase_fix_results = _validate_phase_fix(red_only, phase_fix_net_file, phase_fix_traci_map)
    phase_fix_promoted = phase_fix_results[phase_fix_results["final_augmented_class"] == "PHASE_FIX_CONFIRMED"].copy()
    phase_fix_unsafe = phase_fix_results[phase_fix_results["final_augmented_class"] == "PHASE_FIX_UNSAFE_CONFLICT"].copy()

    phase_fix_promoted_total = phase_fix_promoted.copy()
    phase_fix_promoted_total["promotion_source"] = "PHASE_FIX"
    phase6_total = pd.concat([prev_confirmed, phase_fix_promoted_total], ignore_index=True)
    if "crosswalk_id" in phase6_total.columns:
        phase6_total = phase6_total.drop_duplicates(subset=["crosswalk_id"], keep="first")

    red_only.to_csv(out_dir / "red_only_phase_fix_input.csv", index=False)
    phase_fix_results.to_csv(out_dir / "red_only_phase_fix_results.csv", index=False)
    phase_fix_promoted.to_csv(out_dir / "phase_fix_promoted_candidates.csv", index=False)
    phase_fix_unsafe.to_csv(out_dir / "phase_fix_unsafe_or_failed.csv", index=False)
    phase6_total.to_csv(out_dir / "phase6_ready_candidates_total_updated.csv", index=False)

    summary_lines = [
        "# red only phase fix batch summary",
        "",
        f"- output_dir: `{out_dir}`",
        f"- input_csv: `{input_csv}`",
        f"- previous_ready: `{previous_ready}`",
        f"- red_only_input_rows: {len(red_only)}",
        f"- phase_fix_promoted: {len(phase_fix_promoted)}",
        f"- phase_fix_unsafe: {len(phase_fix_unsafe)}",
        f"- phase6_total_after: {len(phase6_total)}",
        f"- load_ok: {phase_fix_load_ok}",
        f"- load_err: {phase_fix_load_err or ''}",
        f"- traci_ok: {phase_fix_traci_ok}",
        f"- traci_err: {phase_fix_traci_err or ''}",
        "",
        "## outcomes",
    ]
    for outcome, count in phase_fix_results["final_augmented_class"].value_counts(dropna=False).items():
        summary_lines.append(f"- {outcome}: {int(count)}")
    (out_dir / "batch_summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    metadata = {
        "timestamp": _now_tag(),
        "input_csv": str(input_csv),
        "previous_ready": str(previous_ready),
        "base_net": str(base_net_file),
        "output_dir": str(out_dir),
        "base_net": str(base_net_file),
        "red_only_input_rows": int(len(red_only)),
        "phase_fix_promoted": int(len(phase_fix_promoted)),
        "phase_fix_unsafe": int(len(phase_fix_unsafe)),
        "phase6_total_after": int(len(phase6_total)),
        "load_ok": bool(phase_fix_load_ok),
        "load_err": phase_fix_load_err,
        "traci_ok": bool(phase_fix_traci_ok),
        "traci_err": phase_fix_traci_err,
    }
    (out_dir / "batch_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"output_dir: {out_dir}")
    print(f"red_only_input_rows: {len(red_only)}")
    print(f"phase_fix_promoted: {len(phase_fix_promoted)}")
    print(f"phase_fix_unsafe: {len(phase_fix_unsafe)}")
    print(f"phase6_total_after: {len(phase6_total)}")
    print(f"previous_ready: {previous_ready}")
    print(f"base_net: {base_net_file}")


if __name__ == "__main__":
    main()
