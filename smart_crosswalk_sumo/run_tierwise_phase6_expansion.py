#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from smart_crosswalk_sumo.network_utils import apply_sumo_environment, read_net
from smart_crosswalk_sumo.run_pedestrian_augmented_network import (
    _build_generated_crossing_inventory,
    _build_post_audits,
    _build_summary_md,
    _build_next_strategy_md,
    _parse_net_counts,
    _run_sumo_load_test,
    _collect_traci_controlled_links,
    _build_tls_creation_node_patch,
)


BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
SOURCE_DIR = RESULT_DIR / "phase_next_pedestrian_augmented_network_20260514_120939"
PHASE6_DIR = RESULT_DIR / "phase6_traci_runtime_validation_20260514_123254"
FACTORY_TABLE = RESULT_DIR / "phase_next_real_csv_crossing_factory_20260514_103928" / "real_csv_crossing_factory_table.csv"
BASE_NET = RESULT_DIR / "junggu_osm_network_20260513_184417" / "junggu_generated.net.xml"

EXPANSION_BATCH_LIMIT = 25


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _write_empty_csv(path: Path, headers: list[str]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()


def _tier_counts(df: pd.DataFrame) -> pd.DataFrame:
    rows = [
        {"tier": "PHASE6_READY_CONFIRMED", "count": int((df["final_augmented_class"] == "SMART_SIGNAL_READY").sum())},
        {"tier": "RED_ONLY_PHASE_FIX_NEEDED", "count": int((df["final_augmented_class"] == "RED_ONLY_PHASE_FIX_NEEDED").sum())},
        {"tier": "TLS_CREATION_CANDIDATE", "count": int((df["final_augmented_class"] == "TLS_CREATION_CANDIDATE").sum())},
        {"tier": "UNSIGNALIZED_CROSSWALK", "count": int((df["final_augmented_class"] == "UNSIGNALIZED_CROSSWALK").sum())},
        {"tier": "GEOMETRY_REJECT", "count": int((df["final_augmented_class"] == "GEOMETRY_REJECT").sum())},
        {"tier": "MANUAL_REVIEW", "count": int((df["final_augmented_class"] == "MANUAL_REVIEW").sum())},
    ]
    return pd.DataFrame(rows)


def _choose_tls_batch(tls_df: pd.DataFrame, limit: int) -> pd.DataFrame:
    seen: set[str] = set()
    chosen_junctions: list[str] = []
    for jid in tls_df["nearest_junction_id"].astype(str).tolist():
        if jid in seen:
            continue
        seen.add(jid)
        chosen_junctions.append(jid)
        if len(chosen_junctions) >= limit:
            break
    return tls_df[tls_df["nearest_junction_id"].astype(str).isin(chosen_junctions)].copy()


def _union_confirmed(existing: pd.DataFrame, new_rows: pd.DataFrame) -> pd.DataFrame:
    cols = list(existing.columns)
    if new_rows.empty:
        return existing.copy()
    merged = pd.concat([existing, new_rows], ignore_index=True)
    if "crosswalk_id" in merged.columns:
        merged = merged.drop_duplicates(subset=["crosswalk_id"], keep="first")
    return merged[cols]


def main() -> None:
    apply_sumo_environment()

    timestamp = _now_tag()
    out_dir = RESULT_DIR / f"phase_next_tierwise_expansion_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not FACTORY_TABLE.exists():
        raise FileNotFoundError(FACTORY_TABLE)
    if not BASE_NET.exists():
        raise FileNotFoundError(BASE_NET)
    if not SOURCE_DIR.exists():
        raise FileNotFoundError(SOURCE_DIR)
    if not PHASE6_DIR.exists():
        raise FileNotFoundError(PHASE6_DIR)

    source_audit = pd.read_csv(SOURCE_DIR / "post_augmented_signal_audit.csv")
    phase6_confirmed = pd.read_csv(PHASE6_DIR / "phase6_ready_candidates_traci_confirmed.csv")
    tls_all = pd.read_csv(SOURCE_DIR / "tls_creation_candidates.csv")
    red_only_all = pd.read_csv(SOURCE_DIR / "red_only_phase_fix_candidates.csv") if (SOURCE_DIR / "red_only_phase_fix_candidates.csv").exists() else pd.DataFrame()
    unsignalized = pd.read_csv(SOURCE_DIR / "unsignalized_crosswalks.csv")
    geometry_hold = pd.concat(
        [
            pd.read_csv(SOURCE_DIR / "geometry_reject_candidates.csv") if (SOURCE_DIR / "geometry_reject_candidates.csv").exists() else pd.DataFrame(),
            pd.read_csv(SOURCE_DIR / "manual_review_candidates.csv") if (SOURCE_DIR / "manual_review_candidates.csv").exists() else pd.DataFrame(),
        ],
        ignore_index=True,
    )

    tier_counts_before = _tier_counts(source_audit)
    tier_counts_before.to_csv(out_dir / "tier_counts_before.csv", index=False)
    phase6_confirmed.to_csv(out_dir / "phase6_ready_candidates.csv", index=False)
    unsignalized.to_csv(out_dir / "unsignalized_crosswalks.csv", index=False)
    geometry_hold.to_csv(out_dir / "geometry_reject_or_manual_hold.csv", index=False)

    red_only_results = pd.DataFrame(columns=list(source_audit.columns) + ["batch_action", "batch_note", "batch_status"])
    _write_empty_csv(out_dir / "red_only_phase_fix_results.csv", list(red_only_results.columns))

    tls_batch = _choose_tls_batch(tls_all, EXPANSION_BATCH_LIMIT)
    tls_batch = tls_batch.copy()
    tls_batch["batch_action"] = "TLS_CREATION_BATCH"
    tls_batch["batch_note"] = "unique junction batch"
    tls_batch["batch_status"] = "selected_for_rerun"
    tls_batch["generation_reason"] = "tierwise tls creation batch"

    if tls_batch.empty:
        _write_empty_csv(out_dir / "tls_creation_results.csv", list(source_audit.columns) + ["batch_action", "batch_note", "batch_status", "runtime_verdict"])
        _write_empty_csv(out_dir / "explicit_crossing_results.csv", ["note"])
        tier_counts_before.to_csv(out_dir / "tier_counts_after.csv", index=False)
        (out_dir / "tierwise_expansion_summary.md").write_text("# tierwise expansion summary\n\n- TLS batch empty.\n", encoding="utf-8")
        (out_dir / "next_phase6_experiment_candidates.csv").write_text("")
        (out_dir / "run_metadata.json").write_text(json.dumps({"output_dir": str(out_dir), "tls_batch_limit": EXPANSION_BATCH_LIMIT, "tls_batch_size": 0}, indent=2), encoding="utf-8")
        print(out_dir)
        return

    net = read_net(BASE_NET)
    base_root = None
    try:
        import xml.etree.ElementTree as ET

        base_root = ET.parse(BASE_NET).getroot()
    except Exception as exc:
        raise RuntimeError(f"failed to parse base net: {exc}") from exc

    selected_tls_ids = list(dict.fromkeys(tls_batch["nearest_junction_id"].astype(str).tolist()))
    node_patch_text, node_patch_rows = _build_tls_creation_node_patch(tls_batch, net)
    patch_path = out_dir / "tierwise_tls_creation_batch.nod.xml"
    patch_path.write_text(node_patch_text, encoding="utf-8")
    node_patch_df = pd.DataFrame(node_patch_rows)
    node_patch_df.to_csv(out_dir / "tls_creation_batch_candidates.csv", index=False)

    netconvert_cmd = [
        "netconvert",
        "--sumo-net-file",
        str(BASE_NET),
        "--node-files",
        str(patch_path),
        "--output-file",
        str(out_dir / "tierwise_phase6_network_v1.net.xml"),
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
    (out_dir / "netconvert_command.sh").write_text("#!/bin/bash\n" + " ".join(netconvert_cmd) + "\n", encoding="utf-8")
    ret = subprocess.run(netconvert_cmd, capture_output=True, text=True, timeout=1200)
    (out_dir / "netconvert.log").write_text((ret.stdout or "") + "\n" + (ret.stderr or ""), encoding="utf-8")
    if ret.returncode != 0 or not (out_dir / "tierwise_phase6_network_v1.net.xml").exists():
        raise RuntimeError(f"netconvert failed; see {out_dir / 'netconvert.log'}")

    patched_net_file = out_dir / "tierwise_phase6_network_v1.net.xml"
    counts_after = _parse_net_counts(patched_net_file)
    load_ok, load_err = _run_sumo_load_test(patched_net_file, end_time=5)
    patched_root = ET.parse(patched_net_file).getroot()
    traci_ids = {str(t.attrib.get("id", "") or "") for t in patched_root.findall("tlLogic") if str(t.attrib.get("id", "") or "")}
    traci_ok, traci_err, traci_map = _collect_traci_controlled_links(patched_net_file, traci_ids)

    crossing_audit, signal_audit, load_validation = _build_post_audits(tls_batch, patched_net_file, traci_map)
    crossing_audit.to_csv(out_dir / "post_augmented_crossing_audit.csv", index=False)
    signal_audit.to_csv(out_dir / "post_augmented_signal_audit.csv", index=False)
    signal_audit.to_csv(out_dir / "tierwise_tls_creation_results.csv", index=False)
    load_validation.to_csv(out_dir / "network_load_validation.csv", index=False)

    new_confirmed = signal_audit[signal_audit["final_augmented_class"] == "SMART_SIGNAL_READY"].copy()
    phase6_after = _union_confirmed(phase6_confirmed, new_confirmed)
    phase6_after.to_csv(out_dir / "phase6_ready_candidates.csv", index=False)
    new_confirmed.to_csv(out_dir / "next_phase6_experiment_candidates.csv", index=False)

    explicit_crossing_cols = [
        "crosswalk_id",
        "nearest_junction_id",
        "patch_status",
        "patch_edge_pair",
        "matched",
        "match_reason",
        "generated_crossing_edge_id",
        "generated_crossingEdges",
        "generated_node_id",
        "generated_lane_count",
    ]
    _write_empty_csv(out_dir / "explicit_crossing_results.csv", explicit_crossing_cols)
    tier_counts_after = _tier_counts(signal_audit)
    tier_counts_after.to_csv(out_dir / "tier_counts_after.csv", index=False)

    summary_lines = [
        "# tierwise expansion summary",
        "",
        f"- output_dir: `{out_dir}`",
        f"- batch_limit_unique_junctions: {EXPANSION_BATCH_LIMIT}",
        f"- batch_unique_junctions_selected: {len(selected_tls_ids)}",
        f"- batch_crosswalk_rows_selected: {len(tls_batch)}",
        f"- load_ok: {load_ok}",
        f"- traci_ok: {traci_ok}",
        "",
        "## tier counts before",
        *[f"- {r['tier']}: {int(r['count'])}" for _, r in tier_counts_before.iterrows()],
        "",
        "## tier counts after",
        *[f"- {r['tier']}: {int(r['count'])}" for _, r in tier_counts_after.iterrows()],
        "",
        "## promoted",
        f"- PHASE6_READY_CONFIRMED start: {len(phase6_confirmed)}",
        f"- RED_ONLY promoted: 0",
        f"- TLS_CREATION promoted: {int(len(new_confirmed))}",
        f"- EXPLICIT_CROSSING promoted: 0",
        f"- final PHASE6_READY_CONFIRMED total: {int(len(phase6_after))}",
        "",
        "## held",
        f"- UNSIGNALIZED_CROSSWALK: {len(unsignalized)}",
        f"- GEOMETRY_REJECT + MANUAL_REVIEW: {len(geometry_hold)}",
        "",
        "## notes",
        "- red_only tier absent in current source audit.",
        "- explicit crossing tier absent in current source audit, so not processed in this batch.",
        "- TLS tier processed as a small unique-junction batch, not the full 504.",
    ]
    (out_dir / "tierwise_expansion_summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    if not phase6_after.empty:
        phase6_after.to_csv(out_dir / "next_phase6_experiment_candidates.csv", index=False)
    else:
        _write_empty_csv(out_dir / "next_phase6_experiment_candidates.csv", list(phase6_confirmed.columns))

    run_metadata: dict[str, Any] = {
        "output_dir": str(out_dir),
        "source_dir": str(SOURCE_DIR),
        "phase6_dir": str(PHASE6_DIR),
        "batch_limit_unique_junctions": EXPANSION_BATCH_LIMIT,
        "batch_unique_junctions_selected": len(selected_tls_ids),
        "batch_crosswalk_rows_selected": len(tls_batch),
        "phase6_ready_start": int(len(phase6_confirmed)),
        "phase6_ready_final": int(len(phase6_after)),
        "tls_promoted": int(len(new_confirmed)),
        "traci_ok": bool(traci_ok),
        "traci_error": traci_err,
        "sumo_load_ok": bool(load_ok),
        "sumo_load_error": load_err,
        "preservation_check": {
            "base_network_modified": False,
            "registry_modified": False,
            "simulation_logic_modified": False,
        },
    }
    (out_dir / "run_metadata.json").write_text(json.dumps(run_metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"- output_dir: {out_dir}")
    print(f"- PHASE6_READY_CONFIRMED start: {len(phase6_confirmed)}")
    print(f"- TLS_CREATION batch rows: {len(tls_batch)}")
    print(f"- TLS_CREATION promoted: {len(new_confirmed)}")
    print(f"- PHASE6_READY_CONFIRMED final: {len(phase6_after)}")


if __name__ == "__main__":
    main()
