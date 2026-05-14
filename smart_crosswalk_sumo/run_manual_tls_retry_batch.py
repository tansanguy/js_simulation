#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pandas as pd

from smart_crosswalk_sumo.network_utils import apply_sumo_environment, read_net
from smart_crosswalk_sumo.run_max_recovery import (
    BASE_NET,
    FALLBACK_BASE_NET,
    RESULT_DIR,
    _build_row_outcome,
    _build_tls_node_patch,
)
from smart_crosswalk_sumo.run_pedestrian_augmented_network import _collect_traci_controlled_links, _run_sumo_load_test


FACTORY_TABLE = RESULT_DIR / "phase_next_real_csv_crossing_factory_20260514_103928" / "real_csv_crossing_factory_table.csv"
PHASE6_TOTAL_UPDATED = RESULT_DIR / "phase_next_max_recovery_20260514_194109" / "phase6_ready_candidates_total_updated.csv"


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path)


def _write_shell(path: Path, cmd: list[str]) -> None:
    path.write_text("#!/bin/bash\n" + " ".join(cmd) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one manual TLS retry batch from a prepared CSV.")
    parser.add_argument("--input-csv", required=True, help="Prepared batch CSV.")
    parser.add_argument("--output-dir", required=True, help="Run output directory.")
    parser.add_argument(
        "--previous-ready",
        default=str(PHASE6_TOTAL_UPDATED),
        help="Previous cumulative phase6_ready CSV to use as baseline.",
    )
    args = parser.parse_args()

    apply_sumo_environment()

    input_csv = Path(args.input_csv).expanduser().resolve()
    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    batch = _load_csv(input_csv)
    if batch.empty:
        raise ValueError(f"empty batch csv: {input_csv}")

    if "recovery_reason" in batch.columns:
        bad = batch[batch["recovery_reason"] != "TLS_CREATION_RETRY_POSSIBLE"]
        if not bad.empty:
            raise ValueError("input csv contains non TLS_CREATION_RETRY_POSSIBLE rows")

    factory = _load_csv(FACTORY_TABLE)
    factory_view = factory[["crosswalk_id", "lon", "lat", "sumo_x", "sumo_y"]].copy()
    batch = batch.merge(factory_view, on="crosswalk_id", how="left", validate="one_to_one")
    batch["source_bucket"] = "MANUAL_REVIEW_TLS_RETRY_BATCH"

    junction_ids = list(dict.fromkeys(batch["nearest_junction_id"].astype(str).tolist()))
    if len(junction_ids) > 25:
        raise ValueError(f"batch exceeds 25 unique junctions: {len(junction_ids)}")

    base_net_file = BASE_NET if BASE_NET.exists() else FALLBACK_BASE_NET
    if not base_net_file.exists():
        raise FileNotFoundError(base_net_file)

    net = read_net(base_net_file)
    patch_path = _build_tls_node_patch(net, junction_ids, out_dir)
    tls_net_file = out_dir / "recovery_tls_batch_network_v1.net.xml"

    tls_cmd = [
        "netconvert",
        "--sumo-net-file",
        str(base_net_file),
        "--node-files",
        str(patch_path),
        "--output-file",
        str(tls_net_file),
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
    _write_shell(out_dir / "recovery_tls_netconvert_command.sh", tls_cmd)

    tls_ret = subprocess.run(tls_cmd, capture_output=True, text=True, timeout=1200)
    (out_dir / "recovery_tls_netconvert.log").write_text((tls_ret.stdout or "") + "\n" + (tls_ret.stderr or ""), encoding="utf-8")
    if tls_ret.returncode != 0 or not tls_net_file.exists():
        raise RuntimeError(f"netconvert failed; see {out_dir / 'recovery_tls_netconvert.log'}")

    tls_load_ok, tls_load_err = _run_sumo_load_test(tls_net_file, end_time=5)
    root = ET.parse(tls_net_file).getroot()
    tls_ids = {str(t.attrib.get("id", "") or "") for t in root.findall("tlLogic") if str(t.attrib.get("id", "") or "")}

    tls_traci_ok, tls_traci_err, tls_traci_map = _collect_traci_controlled_links(tls_net_file, tls_ids)

    batch_rows = []
    for _, row in batch.iterrows():
        batch_rows.append(_build_row_outcome(row, tls_net_file, tls_traci_map))
    batch_df = pd.DataFrame(batch_rows)
    batch_df.to_csv(out_dir / "recovery_batch_results.csv", index=False)

    promoted_rows = batch_df[batch_df["recovered"] == True].copy()  # noqa: E712
    promoted_rows.to_csv(out_dir / "batch_promoted.csv", index=False)

    previous_ready = Path(args.previous_ready).expanduser().resolve() if args.previous_ready else PHASE6_TOTAL_UPDATED
    if previous_ready.exists():
        phase6_current = _load_csv(previous_ready)
    else:
        phase6_current = pd.DataFrame()
    if not phase6_current.empty and "promotion_source" not in phase6_current.columns:
        phase6_current = phase6_current.copy()
        phase6_current["promotion_source"] = "PREVIOUS"

    updated_total = pd.concat([phase6_current, promoted_rows], ignore_index=True)
    if "crosswalk_id" in updated_total.columns:
        updated_total = updated_total.drop_duplicates(subset=["crosswalk_id"], keep="first")
    updated_total.to_csv(out_dir / "phase6_ready_candidates_total_updated.csv", index=False)

    summary_lines = [
        "# manual tls retry batch summary",
        "",
        f"- output_dir: `{out_dir}`",
        f"- input_csv: `{input_csv}`",
        f"- batch_rows: {len(batch_df)}",
        f"- unique_junctions: {len(junction_ids)}",
        f"- load_ok: {tls_load_ok}",
        f"- load_err: {tls_load_err or ''}",
        f"- traci_ok: {tls_traci_ok}",
        f"- traci_err: {tls_traci_err or ''}",
        f"- promoted_rows: {len(promoted_rows)}",
        f"- phase6_total_before: {len(phase6_current)}",
        f"- phase6_total_after: {len(updated_total)}",
        "",
        "## outcomes",
    ]
    outcome_counts = batch_df["recovery_outcome"].value_counts(dropna=False)
    for outcome, count in outcome_counts.items():
        summary_lines.append(f"- {outcome}: {int(count)}")
    (out_dir / "batch_summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    metadata = {
        "timestamp": _now_tag(),
        "input_csv": str(input_csv),
        "output_dir": str(out_dir),
        "previous_ready": str(previous_ready),
        "base_net": str(base_net_file),
        "factory_table": str(FACTORY_TABLE),
        "batch_rows": int(len(batch_df)),
        "unique_junctions": int(len(junction_ids)),
        "promoted_rows": int(len(promoted_rows)),
        "phase6_total_before": int(len(phase6_current)),
        "phase6_total_after": int(len(updated_total)),
        "load_ok": bool(tls_load_ok),
        "load_err": tls_load_err,
        "traci_ok": bool(tls_traci_ok),
        "traci_err": tls_traci_err,
    }
    (out_dir / "batch_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"output_dir: {out_dir}")
    print(f"batch_rows: {len(batch_df)}")
    print(f"unique_junctions: {len(junction_ids)}")
    print(f"promoted_rows: {len(promoted_rows)}")
    print(f"phase6_total_after: {len(updated_total)}")
    print(f"previous_ready: {previous_ready}")


if __name__ == "__main__":
    main()
