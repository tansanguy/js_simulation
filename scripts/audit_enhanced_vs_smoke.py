#!/usr/bin/env python3
"""
Audit: enhanced smoke baseline/smart 비교 무결성.

기존 (Azure) smoke 결과 디렉토리를 source-of-truth로,
enhanced smoke 결과 디렉토리 구조/컬럼/값 차이를 비교한다.

Usage:
  python scripts/audit_enhanced_vs_smoke.py \
    --enhanced-dir result/active/real_30seed_runs_sampled10/runs_enhanced/current_main_12 \
    --smoke-dir    result/active/real_30seed_runs_sampled10/runs/p1_p4_recovery_6 \
    --crosswalk-id NODE_10335
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

import pandas as pd

KEY_METRICS = [
    "extension_count", "total_extension_sec",
    "avg_queue_length", "max_queue_length",
    "local_500m_vehicle_count", "local_500m_mean_speed",
    "local_500m_mean_time_loss", "local_500m_avg_delay_sec",
    "network_mean_travel_time", "network_arrived_vehicles",
    "pet_min", "pet_p10", "pet_mean",
    "very_risky_crossing_count", "risky_crossing_count",
    "ped_wait_time_mean", "ped_wait_time_max",
    "elderly_incomplete_crossings",
]


def _read_csv(p: Path) -> pd.DataFrame | None:
    if not p.exists():
        return None
    try:
        return pd.read_csv(p)
    except Exception as e:
        print(f"[warn] read fail {p}: {e}", file=sys.stderr)
        return None


def _scan_seed_dirs(scenario_root: Path) -> list[Path]:
    if not scenario_root.exists():
        return []
    return sorted(p for p in scenario_root.iterdir() if p.is_dir() and p.name.startswith("seed"))


def audit_dir(label: str, root: Path, crosswalk_id: str | None) -> dict:
    out: dict = {"label": label, "root": str(root)}
    for scen in ("baseline", "smart"):
        scen_root = root / scen
        seeds = _scan_seed_dirs(scen_root)
        out[f"{scen}_seed_dirs"] = [s.name for s in seeds]
        row_total = 0
        first_df = None
        for sd in seeds:
            df = _read_csv(sd / "simulation_result.csv")
            if df is None:
                continue
            if crosswalk_id:
                df = df[df["crosswalk_id"].astype(str) == crosswalk_id]
            row_total += len(df)
            if first_df is None:
                first_df = df
        out[f"{scen}_row_count"] = row_total
        out[f"{scen}_cols"] = list(first_df.columns) if first_df is not None else []
        out[f"{scen}_df"] = first_df
    # with_baseline — long format: baseline row + smart row per crosswalk
    wb_rows = 0
    wb_df = None
    wb_has_baseline_scenario = False
    wb_has_smart_scenario = False
    delta_cols: list[str] = []
    for sd in _scan_seed_dirs(root / "smart"):
        df = _read_csv(sd / "simulation_result_with_baseline.csv")
        if df is None:
            continue
        if crosswalk_id:
            df = df[df["crosswalk_id"].astype(str) == crosswalk_id]
        wb_rows += len(df)
        if wb_df is None:
            wb_df = df
            delta_cols = [c for c in df.columns if c.startswith("delta_")]
        if "scenario" in df.columns:
            wb_has_baseline_scenario = wb_has_baseline_scenario or ("baseline" in df["scenario"].values)
            wb_has_smart_scenario    = wb_has_smart_scenario    or ("smart"    in df["scenario"].values)
    out["with_baseline_row_count"]      = wb_rows
    out["with_baseline_has_baseline"]   = wb_has_baseline_scenario
    out["with_baseline_has_smart"]      = wb_has_smart_scenario
    out["delta_col_count"] = len(delta_cols)
    out["with_baseline_df"] = wb_df
    return out


def compare_metrics(audit: dict) -> pd.DataFrame:
    # prefer long-format with_baseline_df split by scenario
    wb = audit.get("with_baseline_df")
    if wb is not None and "scenario" in wb.columns:
        b = wb[wb["scenario"] == "baseline"]
        s = wb[wb["scenario"] == "smart"]
        if b.empty:
            b = None
        if s.empty:
            s = None
    else:
        b = audit.get("baseline_df")
        s = audit.get("smart_df")
    rows = []
    for m in KEY_METRICS:
        bv = b[m].iloc[0] if (b is not None and not b.empty and m in b.columns) else None
        sv = s[m].iloc[0] if (s is not None and not s.empty and m in s.columns) else None
        try:
            diff = float(sv) - float(bv) if (bv is not None and sv is not None) else None
        except Exception:
            diff = None
        rows.append({"metric": m, "baseline": bv, "smart": sv, "delta": diff,
                     "same?": (bv == sv)})
    return pd.DataFrame(rows)


def col_diff(audit: dict) -> tuple[list[str], list[str], list[str]]:
    b_cols = set(audit.get("baseline_cols", []))
    s_cols = set(audit.get("smart_cols", []))
    common = sorted(b_cols & s_cols)
    only_b = sorted(b_cols - s_cols)
    only_s = sorted(s_cols - b_cols)
    return common, only_b, only_s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enhanced-dir", required=True, type=Path)
    ap.add_argument("--smoke-dir",    type=Path, default=None,
                    help="Azure 원본 smoke 결과 (선택)")
    ap.add_argument("--crosswalk-id", type=str, default=None)
    args = ap.parse_args()

    print(f"\n=== ENHANCED: {args.enhanced_dir} ===")
    enh = audit_dir("enhanced", args.enhanced_dir, args.crosswalk_id)
    print(f"baseline seed dirs:  {enh['baseline_seed_dirs']}")
    print(f"smart seed dirs:     {enh['smart_seed_dirs']}")
    print(f"baseline row count:  {enh['baseline_row_count']}")
    print(f"smart row count:     {enh['smart_row_count']}")
    print(f"with_baseline rows:  {enh['with_baseline_row_count']}")

    common, only_b, only_s = col_diff(enh)
    print(f"\ncols common (count={len(common)})")
    print(f"cols only baseline ({len(only_b)}): {only_b}")
    print(f"cols only smart    ({len(only_s)}): {only_s}")

    print("\n--- KEY METRIC DIFF (enhanced) ---")
    diff_df = compare_metrics(enh)
    print(diff_df.to_string(index=False))

    same_keys = diff_df[diff_df["same?"] == True]["metric"].tolist()
    diff_keys = diff_df[diff_df["same?"] == False]["metric"].tolist()
    print(f"\nIDENTICAL metrics: {same_keys}")
    print(f"DIFFERING metrics: {diff_keys}")

    # print long-format structure info
    print(f"\nwith_baseline has baseline row: {enh['with_baseline_has_baseline']}")
    print(f"with_baseline has smart row:    {enh['with_baseline_has_smart']}")
    print(f"delta_* cols:                   {enh['delta_col_count']}")

    # show with_baseline content if available
    if enh["with_baseline_df"] is not None:
        wb = enh["with_baseline_df"]
        show_cols = ["crosswalk_id", "scenario"] + [
            c for c in KEY_METRICS + [f"delta_{m}" for m in KEY_METRICS]
            if c in wb.columns
        ]
        print("\n--- with_baseline content ---")
        print(wb[show_cols].to_string(index=False))

    # validation gate (long format)
    fail = []
    if not enh["with_baseline_has_baseline"]:
        fail.append("with_baseline missing baseline scenario row")
    if not enh["with_baseline_has_smart"]:
        fail.append("with_baseline missing smart scenario row")
    expected_wb_rows = enh["smart_row_count"] * 2
    if enh["with_baseline_row_count"] != expected_wb_rows:
        fail.append(
            f"with_baseline row count {enh['with_baseline_row_count']} != expected {expected_wb_rows} (smart×2)"
        )
    if enh["delta_col_count"] == 0:
        fail.append("no delta_* columns")
    # smart==baseline 전체 동일은 fail로 안 보고 warn (실험 설계 이슈)
    if len(diff_keys) == 0:
        print("\n[WARN] smart vs baseline KEY metrics 모두 동일 — 정책 효과 측정 안 됨")

    if args.smoke_dir:
        print(f"\n=== ORIGINAL SMOKE: {args.smoke_dir} ===")
        smk = audit_dir("smoke", args.smoke_dir, args.crosswalk_id)
        print(f"baseline rows: {smk['baseline_row_count']}, smart rows: {smk['smart_row_count']}")
        print(f"with_baseline rows: {smk['with_baseline_row_count']}")
        if smk["baseline_df"] is not None and smk["smart_df"] is not None:
            print("\n--- KEY METRIC DIFF (original smoke) ---")
            print(compare_metrics(smk).to_string(index=False))

    if fail:
        print(f"\n[FAIL] {fail}")
        return 1
    print("\n[OK] structure audit passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
