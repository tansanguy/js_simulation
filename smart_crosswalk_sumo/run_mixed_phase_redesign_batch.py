#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from smart_crosswalk_sumo.network_utils import apply_sumo_environment, read_net
from smart_crosswalk_sumo.run_phase6_recovery_smoke import _phase_aligned_depart_plan

BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
INPUT_CSV = RESULT_DIR / "phase_next_top50_tiered_recovery_audit" / "top50_mixed_phase_redesign_input.csv"
BASE_NET = RESULT_DIR / "phase_next_recovery_command_plan_20260514_200908" / "batch_03_run" / "recovery_tls_batch_network_v1.net.xml"
OUT_DIR = RESULT_DIR / "phase_next_top50_mixed_phase_redesign"
PREFERRED_PED_ONLY_DURATION = 2.0


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text in {"nan", "None"}:
        return ""
    if text.endswith(".0"):
        raw = text[:-2]
        if raw.replace("-", "", 1).isdigit():
            return raw
    return text


def _parse_int(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text or text in {"nan", "None"}:
        return None
    try:
        return int(float(text))
    except Exception:
        return None


def _load_xml(net_file: Path) -> ET.Element:
    return ET.parse(net_file).getroot()


def _green_count(state: str, ped_link_index: int) -> int:
    return sum(1 for idx, ch in enumerate(state) if idx != ped_link_index and ch in {"G", "g"})


def _ped_only_state(state: str, ped_link_index: int) -> str:
    chars = list(state)
    for idx, ch in enumerate(chars):
        if idx != ped_link_index:
            chars[idx] = "r"
    if 0 <= ped_link_index < len(chars):
        chars[ped_link_index] = "G" if chars[ped_link_index] not in {"G", "g"} else chars[ped_link_index]
    return "".join(chars)


def _choose_source_phase(phases: list[ET.Element], ped_link_index: int) -> tuple[int | None, str, int]:
    for idx, phase in enumerate(phases):
        state = str(phase.attrib.get("state", "") or "")
        if ped_link_index >= len(state):
            continue
        if state[ped_link_index] in {"G", "g"}:
            non_ped_green = _green_count(state, ped_link_index)
            if non_ped_green > 0:
                return idx, state, non_ped_green
    return None, "", 0


def _build_redesign_patch(
    base_net_file: Path,
    mixed_df: pd.DataFrame,
    out_dir: Path,
) -> tuple[Path, pd.DataFrame, pd.DataFrame]:
    root = _load_xml(base_net_file)
    tl_by_id = {str(t.get("id", "") or ""): t for t in root.findall("tlLogic") if str(t.get("id", "") or "")}

    patch_root = ET.Element(
        "tlLogics",
        {
            "version": "1.20",
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:noNamespaceSchemaLocation": "http://sumo.dlr.de/xsd/tllogic_file.xsd",
        },
    )

    tls_groups = []
    for tls_id, group in mixed_df.groupby(mixed_df["tls_id_used"].astype(str), sort=False):
        tls_groups.append((tls_id, group.copy()))

    patch_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []

    for tls_id, group in tls_groups:
        base_tl = tl_by_id.get(tls_id)
        if base_tl is None:
            for _, row in group.iterrows():
                candidate_rows.append(
                    {
                        **row.to_dict(),
                        "redesign_possible": False,
                        "redesign_strategy": "missing_tlLogic",
                        "current_phase_index": "",
                        "current_phase_state": "",
                        "current_ped_link_state": "",
                        "non_ped_green_indices": "",
                        "non_ped_green_count": "",
                        "current_ped_green_phase_is_mixed": False,
                        "current_pedestrian_only_phase_exists": False,
                        "patched_ped_only_phase_state": "",
                        "patched_phase_added": False,
                        "patched_phase_count": "",
                        "phase_patch_status": "MISSING_TLLOGIC",
                        "final_tier": "MANUAL_REVIEW",
                        "recommended_action": "manual review",
                        "reason": "missing_tlLogic",
                    }
                )
            continue

        tl_copy = ET.fromstring(ET.tostring(base_tl, encoding="utf-8"))
        phase_elems = tl_copy.findall("phase")
        phase_states = [str(p.attrib.get("state", "") or "") for p in phase_elems]
        phase_durations = [float(p.attrib.get("duration", "0") or 0.0) for p in phase_elems]
        ped_indices = sorted(
            {idx for idx in group["ped_link_index"].dropna().astype(int).tolist() if idx >= 0}
        )
        if not ped_indices:
            for _, row in group.iterrows():
                candidate_rows.append(
                    {
                        **row.to_dict(),
                        "redesign_possible": False,
                        "redesign_strategy": "missing_ped_link_index",
                        "current_phase_index": "",
                        "current_phase_state": "",
                        "current_ped_link_state": "",
                        "non_ped_green_indices": "",
                        "non_ped_green_count": "",
                        "current_ped_green_phase_is_mixed": False,
                        "current_pedestrian_only_phase_exists": False,
                        "patched_ped_only_phase_state": "",
                        "patched_phase_added": False,
                        "patched_phase_count": len(phase_elems),
                        "phase_patch_status": "MISSING_PED_LINK",
                        "final_tier": "MANUAL_REVIEW",
                        "recommended_action": "manual review",
                        "reason": "missing_ped_link_index",
                    }
                )
            continue

        source_phase_by_ped: dict[int, tuple[int, str, int]] = {}
        for ped_idx in ped_indices:
            idx, state, non_ped_green_count = _choose_source_phase(phase_elems, ped_idx)
            if idx is not None:
                source_phase_by_ped[ped_idx] = (idx, state, non_ped_green_count)

        if len(source_phase_by_ped) != len(ped_indices):
            for _, row in group.iterrows():
                ped_idx = int(row["ped_link_index"])
                src = source_phase_by_ped.get(ped_idx)
                candidate_rows.append(
                    {
                        **row.to_dict(),
                        "redesign_possible": False,
                        "redesign_strategy": "no_mixed_source_phase",
                        "current_phase_index": src[0] if src else "",
                        "current_phase_state": src[1] if src else "",
                        "current_ped_link_state": src[1][ped_idx] if src and ped_idx < len(src[1]) else "",
                        "non_ped_green_indices": "",
                        "non_ped_green_count": src[2] if src else "",
                        "current_ped_green_phase_is_mixed": bool(src and src[2] > 0),
                        "current_pedestrian_only_phase_exists": False,
                        "patched_ped_only_phase_state": "",
                        "patched_phase_added": False,
                        "patched_phase_count": len(phase_elems),
                        "phase_patch_status": "NO_MIXED_SOURCE",
                        "final_tier": "MIXED_PHASE_REDESIGN_NEEDED",
                        "recommended_action": "hold for manual review",
                        "reason": "no_mixed_source_phase",
                    }
                )
            continue

        inserted_phase_rows: list[dict[str, Any]] = []
        for ped_idx in ped_indices:
            src_idx, src_state, non_ped_green_count = source_phase_by_ped[ped_idx]
            new_state = _ped_only_state(src_state, ped_idx)
            new_phase = ET.Element(
                "phase",
                {
                    "duration": f"{PREFERRED_PED_ONLY_DURATION:.1f}",
                    "state": new_state,
                },
            )
            tl_copy.append(new_phase)
            inserted_phase_rows.append(
                {
                    "tls_id_used": tls_id,
                    "target_ped_link_index": ped_idx,
                    "source_phase_index": src_idx,
                    "source_phase_state": src_state,
                    "source_non_ped_green_count": non_ped_green_count,
                    "inserted_phase_index": len(phase_elems) + len(inserted_phase_rows),
                    "inserted_phase_state": new_state,
                    "inserted_phase_duration": PREFERRED_PED_ONLY_DURATION,
                    "insertion_strategy": "append_ped_only_phase_from_mixed_template",
                    "phase_count_before": len(phase_elems),
                    "phase_count_after": len(phase_elems) + len(inserted_phase_rows),
                }
            )

        patch_root.append(tl_copy)
        patch_rows.extend(inserted_phase_rows)

        phase_states_after = [str(p.attrib.get("state", "") or "") for p in tl_copy.findall("phase")]
        for _, row in group.iterrows():
            ped_idx = int(row["ped_link_index"])
            src_idx, src_state, non_ped_green_count = source_phase_by_ped[ped_idx]
            inserted_state = _ped_only_state(src_state, ped_idx)
            candidate_rows.append(
                {
                    **row.to_dict(),
                    "redesign_possible": True,
                    "redesign_strategy": "append_ped_only_phase_from_mixed_template",
                    "current_phase_index": src_idx,
                    "current_phase_state": src_state,
                    "current_ped_link_state": src_state[ped_idx] if ped_idx < len(src_state) else "",
                    "non_ped_green_indices": "|".join(
                        str(i) for i, ch in enumerate(src_state) if i != ped_idx and ch in {"G", "g"}
                    ),
                    "non_ped_green_count": non_ped_green_count,
                    "current_ped_green_phase_is_mixed": True,
                    "current_pedestrian_only_phase_exists": False,
                    "patched_ped_only_phase_state": inserted_state,
                    "patched_phase_added": True,
                    "patched_phase_count": len(phase_states_after),
                    "phase_patch_status": "PED_ONLY_PHASE_APPENDED",
                    "final_tier": "PHASE6_READY_PEDESTRIAN_ONLY",
                    "recommended_action": "queue smart/baseline seed 1 smoke",
                    "reason": "pedestrian_only_phase_appended",
                }
            )

    ET.indent(patch_root, space="  ")
    patch_path = out_dir / "mixed_phase_redesign.tll.xml"
    patch_path.write_text(ET.tostring(patch_root, encoding="unicode", xml_declaration=True), encoding="utf-8")
    return patch_path, pd.DataFrame(candidate_rows), pd.DataFrame(patch_rows)


def _audit_redesign_candidates(net_file: Path, candidates: pd.DataFrame) -> pd.DataFrame:
    root = ET.parse(net_file).getroot()
    tl_map = {
        str(t.attrib.get("id", "") or ""): t
        for t in root.findall("tlLogic")
        if str(t.attrib.get("id", "") or "")
    }
    rows: list[dict[str, Any]] = []
    for _, row in candidates.iterrows():
        tls_id = _normalize_text(row.get("tls_id_used"))
        ped_idx = _parse_int(row.get("ped_link_index"))
        tl_elem = tl_map.get(tls_id)
        phases = list(tl_elem.findall("phase")) if tl_elem is not None else []
        phase_states = [str(p.attrib.get("state", "") or "") for p in phases]
        current_state = _normalize_text(row.get("current_phase_state"))
        if not current_state and phases and ped_idx is not None:
            for idx, phase in enumerate(phases):
                state = str(phase.attrib.get("state", "") or "")
                if ped_idx < len(state) and state[ped_idx] in {"G", "g"} and _green_count(state, ped_idx) > 0:
                    current_state = state
                    break
        current_ped_link_state = current_state[ped_idx] if current_state and ped_idx is not None and ped_idx < len(current_state) else ""
        non_ped_green_indices = "|".join(
            str(i) for i, ch in enumerate(current_state) if ped_idx is not None and i != ped_idx and ch in {"G", "g"}
        ) if current_state else ""
        non_ped_green_count = _green_count(current_state, ped_idx) if current_state and ped_idx is not None else 0
        ped_has_green = any(
            ped_idx is not None and ped_idx < len(state) and state[ped_idx] in {"G", "g"}
            for state in phase_states
        )
        ped_has_red = any(
            ped_idx is not None and ped_idx < len(state) and state[ped_idx] == "r"
            for state in phase_states
        )
        pedestrian_only_phase_available = any(
            ped_idx is not None
            and ped_idx < len(state)
            and state[ped_idx] in {"G", "g"}
            and _green_count(state, ped_idx) == 0
            for state in phase_states
        )
        mixed_phase_only = ped_has_green and not pedestrian_only_phase_available
        phase_aligned_depart_possible = bool(
            row.get("route_valid")
            and row.get("contains_crossing_edge")
            and row.get("crossing_present")
            and row.get("walkingarea_present")
            and row.get("tlLogic_present")
            and ped_idx is not None
            and pedestrian_only_phase_available
        )
        final_tier = "PHASE6_READY_PEDESTRIAN_ONLY" if phase_aligned_depart_possible else "MIXED_PHASE_REDESIGN_NEEDED"
        recommended_action = "queue smart/baseline seed 1 smoke" if phase_aligned_depart_possible else "hold for redesign"
        reason = "patched_ped_only_phase_available" if phase_aligned_depart_possible else "no_ped_only_phase_after_patch"
        rows.append(
            {
                **row.to_dict(),
                "tls_id_used": tls_id,
                "ped_link_index": ped_idx,
                "current_phase_index": _parse_int(row.get("current_phase_index")),
                "current_phase_state": current_state,
                "current_ped_link_state": current_ped_link_state,
                "non_ped_green_indices": non_ped_green_indices,
                "non_ped_green_count": non_ped_green_count,
                "current_ped_green_phase_is_mixed": bool(non_ped_green_count > 0),
                "current_pedestrian_only_phase_exists": bool(
                    any(
                        ped_idx is not None
                        and ped_idx < len(state)
                        and state[ped_idx] in {"G", "g"}
                        and _green_count(state, ped_idx) == 0
                        for state in phase_states
                    )
                ),
                "ped_has_green_after_redesign": ped_has_green,
                "ped_has_red_after_redesign": ped_has_red,
                "pedestrian_only_phase_available_after_redesign": pedestrian_only_phase_available,
                "mixed_phase_only_after_redesign": mixed_phase_only,
                "phase_aligned_depart_possible": phase_aligned_depart_possible,
                "final_tier": final_tier,
                "recommended_action": recommended_action,
                "reason": reason,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mixed-phase redesign batch for top50 recovery audit.")
    parser.add_argument("--input-csv", default=str(INPUT_CSV), help="top50 mixed-phase redesign input CSV.")
    parser.add_argument("--base-net", default=str(BASE_NET), help="Base recovery network XML.")
    parser.add_argument("--output-dir", default=str(OUT_DIR), help="Output directory.")
    args = parser.parse_args()

    apply_sumo_environment()
    input_csv = Path(args.input_csv).expanduser().resolve()
    base_net_file = Path(args.base_net).expanduser().resolve()
    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    mixed = pd.read_csv(input_csv)
    if mixed.empty:
        raise FileNotFoundError(input_csv)
    if len(mixed) != 17:
        raise ValueError(f"expected 17 mixed rows, got {len(mixed)}")
    if not base_net_file.exists():
        raise FileNotFoundError(base_net_file)

    mixed = mixed.copy()
    mixed["crosswalk_id"] = mixed["crosswalk_id"].astype(str).map(_normalize_text)
    mixed["source_crosswalk_id"] = mixed["source_crosswalk_id"].astype(str).map(_normalize_text)
    mixed["canonical_crosswalk_id"] = mixed["canonical_crosswalk_id"].astype(str).map(_normalize_text)
    mixed["tls_id_used"] = mixed["tls_id_used"].astype(str).map(_normalize_text)
    mixed["ped_link_index"] = mixed["ped_link_index"].apply(_parse_int)

    patch_path, candidate_rows, patch_rows = _build_redesign_patch(base_net_file, mixed, out_dir)
    new_net_file = out_dir / "mixed_phase_redesign_network.net.xml"
    netconvert_cmd = [
        "netconvert",
        "--sumo-net-file",
        str(base_net_file),
        "--tllogic-files",
        str(patch_path),
        "--output-file",
        str(new_net_file),
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
    (out_dir / "mixed_phase_redesign_netconvert_command.sh").write_text(
        "#!/bin/bash\n" + " ".join(netconvert_cmd) + "\n",
        encoding="utf-8",
    )
    netconvert_ret = subprocess.run(netconvert_cmd, capture_output=True, text=True, timeout=1200)
    (out_dir / "mixed_phase_redesign_netconvert.log").write_text(
        (netconvert_ret.stdout or "") + "\n" + (netconvert_ret.stderr or ""),
        encoding="utf-8",
    )
    if netconvert_ret.returncode != 0 or not new_net_file.exists():
        raise RuntimeError(f"mixed phase netconvert failed; see {out_dir / 'mixed_phase_redesign_netconvert.log'}")

    audit_df = _audit_redesign_candidates(new_net_file, candidate_rows)
    promoted_df = audit_df[audit_df["final_tier"] == "PHASE6_READY_PEDESTRIAN_ONLY"].copy().reset_index(drop=True)
    hold_df = audit_df[audit_df["final_tier"] != "PHASE6_READY_PEDESTRIAN_ONLY"].copy().reset_index(drop=True)

    depart_plan_df = _phase_aligned_depart_plan(
        new_net_file,
        promoted_df,
        ped_repeat_count=5,
        ped_repeat_spacing_sec=2.0,
        ped_depart_offset_sec=None,
    ) if not promoted_df.empty else pd.DataFrame()

    audit_df.to_csv(out_dir / "mixed_phase_redesign_audit.csv", index=False)
    promoted_df.to_csv(out_dir / "mixed_phase_redesign_promoted_pedestrian_only_candidates.csv", index=False)
    hold_df.to_csv(out_dir / "mixed_phase_redesign_failed_or_hold.csv", index=False)
    patch_rows_df = pd.DataFrame(patch_rows)
    patch_rows_df.to_csv(out_dir / "mixed_phase_tllogic_patch_audit.csv", index=False)
    if not depart_plan_df.empty:
        depart_plan_df.to_csv(out_dir / "pedestrian_depart_plan.csv", index=False)

    promoted_single_dir = out_dir / "mixed_phase_redesign_promoted_single_candidate_csv"
    promoted_single_dir.mkdir(parents=True, exist_ok=True)
    for _, row in promoted_df.iterrows():
        cid = str(row["crosswalk_id"])
        row.to_frame().T.to_csv(promoted_single_dir / f"{cid}.csv", index=False)

    smoke_script = out_dir / "run_mixed_promoted_seed1_smoke_commands.sh"
    smoke_lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f'ROOT="{out_dir}"',
        f'NET_FILE="{new_net_file}"',
        'INPUT_DIR="$ROOT/mixed_phase_redesign_promoted_single_candidate_csv"',
        "",
        'for csv in "$INPUT_DIR"/*.csv; do',
        '  cid=$(basename "$csv" .csv)',
        '  python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \\',
        '    --candidate-csv "$csv" \\',
        '    --net-file "$NET_FILE" \\',
        '    --scenario smart \\',
        '    --seed 1 \\',
        '    --sim-duration 600 \\',
        '    --warmup 0 \\',
        '    --step-length 0.5 \\',
        '    --extension-sec 5.0 \\',
        '    --phase-aligned-ped-depart \\',
        '    --ped-repeat-count 5 \\',
        '    --ped-repeat-spacing-sec 2 \\',
        '    --output-dir "$ROOT/smoke_seed1_smart_${cid}"',
        '  python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \\',
        '    --candidate-csv "$csv" \\',
        '    --net-file "$NET_FILE" \\',
        '    --scenario baseline \\',
        '    --seed 1 \\',
        '    --sim-duration 600 \\',
        '    --warmup 0 \\',
        '    --step-length 0.5 \\',
        '    --extension-sec 5.0 \\',
        '    --phase-aligned-ped-depart \\',
        '    --ped-repeat-count 5 \\',
        '    --ped-repeat-spacing-sec 2 \\',
        '    --output-dir "$ROOT/smoke_seed1_baseline_${cid}"',
        "done",
    ]
    smoke_script.write_text("\n".join(smoke_lines) + "\n", encoding="utf-8")
    smoke_script.chmod(0o755)

    check_script = out_dir / "check_mixed_promoted_seed1_smoke_results.py"
    check_script.write_text(
        """#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent


def _extension_count(dir_path: Path, scenario: str) -> float:
    summary = dir_path / "phase6_smoke_summary.csv"
    if not summary.exists():
        return float("nan")
    df = pd.read_csv(summary)
    row = df[df["scenario"].astype(str) == scenario]
    if row.empty or "extension_count" not in row.columns:
        return float("nan")
    return float(row["extension_count"].iloc[0])


def main() -> None:
    promoted = pd.read_csv(ROOT / "mixed_phase_redesign_promoted_pedestrian_only_candidates.csv")
    hold = pd.read_csv(ROOT / "mixed_phase_redesign_failed_or_hold.csv")
    print(f"promoted_rows: {len(promoted)}")
    print(f"failed_or_hold_rows: {len(hold)}")
    if not promoted.empty:
        print(promoted[["crosswalk_id", "final_tier", "reason"]].to_string(index=False))
    smart_dirs = sorted(ROOT.glob("smoke_seed1_smart_*"))
    baseline_dirs = sorted(ROOT.glob("smoke_seed1_baseline_*"))
    print(f"smart_dirs: {len(smart_dirs)}")
    print(f"baseline_dirs: {len(baseline_dirs)}")
    for d in smart_dirs[:5]:
        print(f"smart {d.name}: extension_count={_extension_count(d, 'smart')}")
    for d in baseline_dirs[:5]:
        print(f"baseline {d.name}: extension_count={_extension_count(d, 'baseline')}")


if __name__ == "__main__":
    main()
""",
        encoding="utf-8",
    )
    check_script.chmod(0o755)

    summary_lines = [
        "# mixed phase redesign summary",
        "",
        f"- input rows: {len(mixed)}",
        f"- promoted rows: {len(promoted_df)}",
        f"- failed_or_hold rows: {len(hold_df)}",
        f"- net file: `{new_net_file}`",
        f"- smoke command script: `{smoke_script}`",
        f"- check script: `{check_script}`",
        "",
        "## promoted crosswalk_id",
    ]
    if promoted_df.empty:
        summary_lines.append("- none")
    else:
        for cid in promoted_df["crosswalk_id"].astype(str).tolist():
            summary_lines.append(f"- {cid}")
    summary_lines.extend(
        [
            "",
            "## hold/reject crosswalk_id",
        ]
    )
    if hold_df.empty:
        summary_lines.append("- none")
    else:
        for _, row in hold_df.iterrows():
            summary_lines.append(f"- {row['crosswalk_id']}: {row['reason']}")
    summary_lines.extend(
        [
            "",
            "## patch rows",
        ]
    )
    for _, row in patch_rows_df.iterrows():
        summary_lines.append(
            f"- tls_id={row['tls_id_used']}, ped_link_index={row['target_ped_link_index']}, "
            f"source_phase_index={row['source_phase_index']}, inserted_state={row['inserted_phase_state']}"
        )
    summary_lines.extend(
        [
            "",
            "## counts",
            f"- unique tls patched: {promoted_df['tls_id_used'].astype(str).nunique() if not promoted_df.empty else 0}",
            f"- unique ped indices patched: {patch_rows_df['target_ped_link_index'].nunique() if not patch_rows_df.empty else 0}",
            f"- phase6_ready after redesign: {len(promoted_df)}",
        ]
    )
    (out_dir / "mixed_phase_redesign_summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    print(f"mixed input rows: {len(mixed)}")
    print(f"redesign promoted rows: {len(promoted_df)}")
    print(f"failed_or_hold rows: {len(hold_df)}")
    print("promoted crosswalk_id 목록:")
    if promoted_df.empty:
        print("- none")
    else:
        print("\n".join(promoted_df["crosswalk_id"].astype(str).tolist()))
    print("failed/hold crosswalk_id 목록과 reason:")
    if hold_df.empty:
        print("- none")
    else:
        for _, row in hold_df.iterrows():
            print(f"- {row['crosswalk_id']}: {row['reason']}")
    print(f"new net.xml path: {new_net_file}")
    print(f"smoke command path: {smoke_script}")
    print(f"check script path: {check_script}")


if __name__ == "__main__":
    main()
