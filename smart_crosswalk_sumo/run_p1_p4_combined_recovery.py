#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd

from smart_crosswalk_sumo.network_utils import pedestrian_link_indices, read_net
from smart_crosswalk_sumo.run_p4_manual_edge_pair_recovery import (
    _build_crossing_patch,
    _build_tllogic_patch,
    _candidate_table_from_audit,
    _crossing_audit,
    _csv,
    _edge_pair_from_text,
    _find_crossing_edge_id,
    _norm,
    _run_cmd,
    _safe_float,
    _safe_int,
    _write_smoke_command,
    _netconvert_cmd,
    _netconvert_tll_cmd,
)


BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
DEFAULT_P1_NET = RESULT_DIR / "phase_next_remaining22_p1_signal_fix_20260516_010953" / "remaining_p1_signal_fix_net_v1.net.xml"
DEFAULT_P1_CANDIDATE_TABLE = RESULT_DIR / "phase_next_remaining22_p1_signal_fix_20260516_010953" / "remaining_p1_signal_fix_candidate_table.csv"
DEFAULT_P4_PLAN_ROOT = RESULT_DIR / "phase_next_p1_p4_recovery_experiment_plan_20260516_013716"
DEFAULT_P4_SELECTION = DEFAULT_P4_PLAN_ROOT / "csv" / "manual_edge_pair_selection.csv"
DEFAULT_P4_SHORTLIST = DEFAULT_P4_PLAN_ROOT / "csv" / "p4_tls_incident_edge_pair_candidates.csv"

APPROVED_CROSSWALKS = ["NODE_5830", "NODE_5938"]


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _ensure_dirs(root: Path) -> dict[str, Path]:
    subdirs: dict[str, Path] = {}
    for name in ["csv", "commands", "readme", "patches"]:
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        subdirs[name] = path
    return subdirs


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path)


def _empty_to_blank(v: Any) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    return str(v).strip()


def _approved_selection_from_shortlist(
    template_csv: Path,
    shortlist_csv: Path,
    output_csv: Path,
) -> pd.DataFrame:
    template_df = _read_csv(template_csv)
    shortlist_df = _read_csv(shortlist_csv)
    approved_df = shortlist_df[shortlist_df["recommended_review_status"].astype(str).str.upper() == "APPROVE"].copy()
    approved_df = approved_df[approved_df["crosswalk_id"].astype(str).isin(APPROVED_CROSSWALKS)]
    if approved_df.empty:
        raise RuntimeError("no APPROVED shortlist rows for expected P4 crosswalks")

    fill_map: dict[str, dict[str, Any]] = {}
    for _, row in approved_df.iterrows():
        fill_map[_norm(row.get("crosswalk_id"))] = {
            "selected_node_id": _norm(row.get("selected_node_id_candidate")),
            "selected_tls_id": _norm(row.get("selected_tls_id_candidate")),
            "selected_crossing_edges": f"{_norm(row.get('edge_a'))} {_norm(row.get('edge_b'))}".strip(),
            "selected_from_edge": _norm(row.get("edge_a")),
            "selected_to_edge": _norm(row.get("edge_b")),
            "needs_new_signal": "FALSE",
            "confidence": "HIGH",
            "manual_reason": "Approved from v2 realistic incident pair shortlist",
            "review_status": "APPROVED",
            "manual_strategy": _norm(row.get("recommended_manual_strategy")) or "EXISTING_TLS_RETROFIT",
        }

    out = template_df.copy()
    for idx, row in out.iterrows():
        cid = _norm(row.get("crosswalk_id"))
        if cid not in fill_map:
            continue
        fill = fill_map[cid]
        for key, value in fill.items():
            out.at[idx, key] = value
    out.to_csv(output_csv, index=False)
    return out


def _p1_validation_rows(p1_df: pd.DataFrame, net_file: Path) -> pd.DataFrame:
    net = read_net(net_file)
    root = ET.parse(net_file).getroot()
    tl_ids = {tl.getID() for tl in net.getTrafficLights()}
    tl_logic_ids = {_norm(tl.attrib.get("id")) for tl in root.findall("tlLogic")}
    rows: list[dict[str, Any]] = []
    for _, row in p1_df.iterrows():
        cid = _norm(row.get("crosswalk_id"))
        tls_id = _norm(row.get("tls_id_used"))
        crossing_id = _norm(row.get("crossing_edge_id"))
        from_edge = _norm(row.get("route_from_edge"))
        to_edge = _norm(row.get("route_to_edge"))
        crossing_exists = bool(crossing_id and crossing_id in {e.getID() for e in net.getEdges()})
        tls_exists = bool(tls_id and tls_id in tl_ids)
        ped_indices = pedestrian_link_indices(net_file, tls_id, crossing_id, xml_root=root) if tls_exists and crossing_exists else []
        tls_connected = bool(ped_indices)
        route_available = False
        if from_edge and to_edge:
            try:
                path, _ = net.getShortestPath(net.getEdge(from_edge), net.getEdge(to_edge), vClass="pedestrian", withInternal=True)
                route_available = bool(path)
            except Exception:
                route_available = False
        tl_logic_present = bool(tls_id and tls_id in tl_logic_ids)
        preserved = bool(crossing_exists and tls_connected and route_available and tl_logic_present)
        rows.append(
            {
                "crosswalk_id": cid,
                "crossing_edge_id": crossing_id,
                "tls_id_used": tls_id,
                "crossing_exists": crossing_exists,
                "tls_exists": tls_exists,
                "tls_connected": tls_connected,
                "ped_link_index_available": bool(ped_indices),
                "route_available": route_available,
                "tlLogic_present": tl_logic_present,
                "preserved": preserved,
            }
        )
    return pd.DataFrame(rows)


def _combined_validation_rows(p1_rows: pd.DataFrame, p4_rows: pd.DataFrame) -> pd.DataFrame:
    p1 = p1_rows.copy()
    p1.insert(1, "group", "P1")
    p4 = p4_rows.copy()
    p4.insert(1, "group", "P4")
    cols = ["crosswalk_id", "group", "crossing_exists", "tls_connected", "ped_link_index_available", "route_available", "tlLogic_present", "preserved"]
    out = pd.concat([p1, p4], ignore_index=True, sort=False)
    return out.reindex(columns=[c for c in cols if c in out.columns] + [c for c in out.columns if c not in cols])


def _p4_validation_rows(audit_df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, row in audit_df.iterrows():
        rows.append(
            {
                "crosswalk_id": _norm(row.get("crosswalk_id")),
                "crossing_edge_id": _norm(row.get("crossing_edge_id")),
                "selected_node_id": _norm(row.get("selected_node_id")),
                "selected_tls_id": _norm(row.get("selected_tls_id")),
                "crossing_exists": bool(row.get("crossing_exists")),
                "tls_connected": bool(row.get("tls_connected")),
                "ped_link_index_available": bool(row.get("ped_link_index_available")),
                "route_available": bool(row.get("route_available")),
                "tlLogic_present": bool(row.get("tlLogic_present")),
                "preserved": bool(row.get("crossing_exists") and row.get("tls_connected") and row.get("ped_link_index_available") and row.get("route_available") and row.get("tlLogic_present")),
            }
        )
    return pd.DataFrame(rows)


def _build_combined_candidate_table(p1_df: pd.DataFrame, p4_candidate_df: pd.DataFrame, final_net: Path) -> pd.DataFrame:
    p1 = p1_df.copy()
    p1["batch_network_file"] = str(final_net.resolve())
    p1["source_file"] = "remaining_p1_signal_fix_candidate_table.csv"
    p1["final_verdict"] = p1["final_verdict"].astype(str)
    p1["step_test_ok"] = p1["step_test_ok"].astype(str)
    p1["crossing_inventory_used"] = p1["crossing_inventory_used"].astype(bool)
    p1 = p1.reindex(columns=p4_candidate_df.columns if not p4_candidate_df.empty else p1.columns)
    combined = pd.concat([p1, p4_candidate_df], ignore_index=True, sort=False)
    return combined


def _write_readme(out_path: Path, p1_count: int, p4_count: int, total_count: int) -> None:
    text = f"""# P1 + P4 combined recovery

- base: `remaining_p1_signal_fix_net_v1.net.xml`
- P4 approved only: {p4_count}
- P1 preserved rows: {p1_count}
- total candidate rows: {total_count}
- no current_main_12 / signal_fix_9 / generated_signal_7 merge
- no seed1 smoke run yet

## rule
- P1 net stays base.
- P4 only add APPROVED rows.
- P4 HOLD rows stay out.
- final net separate from other experiment groups.
"""
    out_path.write_text(text + "\n", encoding="utf-8")


def _write_smoke_commands(out_dir: Path, candidate_csv: Path, net_file: Path) -> tuple[Path, Path]:
    run_path = out_dir / "commands" / "command_to_run_p1_p4_seed1_smoke.sh"
    check_path = out_dir / "commands" / "command_to_check_p1_p4_seed1_smoke.sh"
    run_path.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
cd "{BASE_DIR}"

export SUMO_HOME="/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO"
export PATH="$SUMO_HOME/bin:$PATH"
export PROJ_LIB="/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO/framework/EclipseSUMO.framework/Resources/proj"
export PYTHONPATH="{BASE_DIR}:${{PYTHONPATH:-}}"

CSV="{candidate_csv.resolve()}"
NET="{net_file.resolve()}"

python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \\
  --candidate-csv "$CSV" \\
  --net-file "$NET" \\
  --scenario smart \\
  --seed 1 \\
  --sim-duration 300 \\
  --phase-aligned-ped-depart \\
  --output-dir "{(out_dir / 'patches' / 'p1_p4_seed1_smart_smoke').resolve()}"

python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \\
  --candidate-csv "$CSV" \\
  --net-file "$NET" \\
  --scenario baseline \\
  --seed 1 \\
  --sim-duration 300 \\
  --phase-aligned-ped-depart \\
  --output-dir "{(out_dir / 'patches' / 'p1_p4_seed1_baseline_smoke').resolve()}"
""",
        encoding="utf-8",
    )
    check_path.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
cd "{BASE_DIR}"

python3 - <<'PY'
from pathlib import Path
import pandas as pd

out_dir = Path(r"{out_dir.resolve()}")
cand = out_dir / "csv" / "p1_p4_recovery_candidate_table.csv"
smart = out_dir / "patches" / "p1_p4_seed1_smart_smoke" / "phase6_smoke_summary.csv"
base = out_dir / "patches" / "p1_p4_seed1_baseline_smoke" / "phase6_smoke_summary.csv"

def b(v):
    return str(v).strip().lower() in ("1", "true", "yes", "y")

def i(v, default=0):
    try:
        if pd.isna(v):
            return default
        return int(float(v))
    except Exception:
        return default

if not cand.exists():
    raise SystemExit(f"missing candidate table: {{cand}}")
if not smart.exists() or not base.exists():
    raise SystemExit("missing smoke summary")
print("smoke summaries present")
PY
""",
        encoding="utf-8",
    )
    run_path.chmod(0o755)
    check_path.chmod(0o755)
    return run_path, check_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build p1_p4_recovery net from P1 net + approved P4 only.")
    parser.add_argument("--output-dir", default="", help="Optional output dir. Default: timestamped result dir.")
    parser.add_argument("--p1-net", default=str(DEFAULT_P1_NET))
    parser.add_argument("--p1-candidate-table", default=str(DEFAULT_P1_CANDIDATE_TABLE))
    parser.add_argument("--p4-plan-root", default=str(DEFAULT_P4_PLAN_ROOT))
    parser.add_argument("--manual-selection-csv", default=str(DEFAULT_P4_SELECTION))
    parser.add_argument("--p4-shortlist-csv", default=str(DEFAULT_P4_SHORTLIST))
    args = parser.parse_args()

    out_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else RESULT_DIR / f"phase_next_p1_p4_combined_recovery_{_now_tag()}"
    dirs = _ensure_dirs(out_dir)
    p1_net = Path(args.p1_net).expanduser().resolve()
    p1_table = Path(args.p1_candidate_table).expanduser().resolve()
    selection_csv = Path(args.manual_selection_csv).expanduser().resolve()
    shortlist_csv = Path(args.p4_shortlist_csv).expanduser().resolve()
    if not p1_net.exists():
        raise FileNotFoundError(p1_net)
    if not p1_table.exists():
        raise FileNotFoundError(p1_table)
    if not shortlist_csv.exists():
        raise FileNotFoundError(shortlist_csv)

    template_csv = Path(args.p4_plan_root).expanduser().resolve() / "csv" / "manual_edge_pair_selection_template.csv"
    if not template_csv.exists():
        raise FileNotFoundError(template_csv)

    # Materialize approved selection copy if caller still points at template-only file.
    if not selection_csv.exists():
        selection_csv.parent.mkdir(parents=True, exist_ok=True)
        selection_df = _approved_selection_from_shortlist(template_csv, shortlist_csv, selection_csv)
    else:
        selection_df = _read_csv(selection_csv)
        approved_mask = selection_df["review_status"].astype(str).str.upper() == "APPROVED"
        if approved_mask.sum() == 0:
            selection_df = _approved_selection_from_shortlist(template_csv, shortlist_csv, selection_csv)

    approved = selection_df[selection_df["review_status"].astype(str).str.upper() == "APPROVED"].copy()
    approved = approved[approved["crosswalk_id"].astype(str).isin(APPROVED_CROSSWALKS)].copy()
    approved = approved.sort_values(["crosswalk_id"], kind="mergesort")
    if set(approved["crosswalk_id"].astype(str).tolist()) != set(APPROVED_CROSSWALKS):
        raise RuntimeError(f"APPROVED rows not match expected set: {approved['crosswalk_id'].astype(str).tolist()}")

    p1_df = _read_csv(p1_table)
    p1_df = p1_df.copy()
    p1_df["batch_network_file"] = str(p1_net.resolve())

    net = read_net(p1_net)
    crossing_patch_path, crossing_patch_plan = _build_crossing_patch(approved, net, dirs["patches"])
    pre_net = out_dir / "p1_p4_recovery_pre_tl.net.xml"
    crossing_cmd = _netconvert_cmd(p1_net, crossing_patch_path, pre_net)
    ok, msg = _run_cmd(crossing_cmd, dirs["patches"] / "p1_p4_crossing_netconvert.log")
    if not ok and not pre_net.exists():
        raise RuntimeError(f"crossing netconvert failed: {msg}")
    if not pre_net.exists():
        raise FileNotFoundError(pre_net)

    pre_root = ET.parse(pre_net).getroot()
    tllogic_patch_path = dirs["patches"] / "p1_p4_recovery_tllogic_patch.tll.xml"
    tllogic_plan = _build_tllogic_patch(pre_net, approved, tllogic_patch_path, pre_root)

    final_net = out_dir / "p1_p4_recovery_net.net.xml"
    if not tllogic_plan.empty and any(tllogic_plan["patch_status"].astype(str).str.upper() == "PATCHED"):
        final_cmd = _netconvert_tll_cmd(pre_net, tllogic_patch_path, final_net)
        ok, msg = _run_cmd(final_cmd, dirs["patches"] / "p1_p4_tllogic_netconvert.log")
        if not ok and not final_net.exists():
            raise RuntimeError(f"tllogic netconvert failed: {msg}")
    else:
        import shutil

        shutil.copyfile(pre_net, final_net)

    audit_df = _crossing_audit(final_net, approved)
    p4_validation = _p4_validation_rows(audit_df)
    p1_validation = _p1_validation_rows(p1_df, final_net)
    combined_validation = _combined_validation_rows(p1_validation, p4_validation)

    p1_validation_path = dirs["csv"] / "p1_preservation_on_p1_p4_net.csv"
    p4_validation_path = dirs["csv"] / "p4_added_short_validation.csv"
    combined_validation_path = dirs["csv"] / "p1_p4_combined_short_validation.csv"
    p1_validation.to_csv(p1_validation_path, index=False)
    p4_validation.to_csv(p4_validation_path, index=False)
    combined_validation.to_csv(combined_validation_path, index=False)

    p4_candidate_table = _candidate_table_from_audit(audit_df, approved, final_net)
    p1_candidate = p1_df.copy()
    p1_candidate["batch_network_file"] = str(final_net.resolve())
    p1_candidate["source_file"] = "remaining_p1_signal_fix_candidate_table.csv"
    p1_candidate["final_verdict"] = p1_candidate["final_verdict"].astype(str)
    p1_candidate["step_test_ok"] = p1_candidate["step_test_ok"].astype(str)
    p1_candidate["crossing_inventory_used"] = p1_candidate["crossing_inventory_used"].astype(bool)
    combined_candidate = pd.concat([p1_candidate, p4_candidate_table], ignore_index=True, sort=False)
    combined_candidate_path = dirs["csv"] / "p1_p4_recovery_candidate_table.csv"
    combined_candidate.to_csv(combined_candidate_path, index=False)

    run_cmd, check_cmd = _write_smoke_commands(out_dir, combined_candidate_path, final_net)
    _write_readme(dirs["readme"] / "p1_p4_combined_recovery_summary.md", len(p1_validation), len(p4_validation), len(combined_candidate))

    print(f"output_dir: {out_dir}")
    print(f"combined_net: {final_net}")
    print(f"candidate_rows: {len(combined_candidate)}")
    print(f"p1_preserved: {int(p1_validation['preserved'].sum()) if not p1_validation.empty else 0}")
    print(f"p4_added_ok: {int(p4_validation['preserved'].sum()) if not p4_validation.empty else 0}")
    print(f"run_smoke_cmd: {run_cmd}")
    print(f"check_smoke_cmd: {check_cmd}")


if __name__ == "__main__":
    main()
