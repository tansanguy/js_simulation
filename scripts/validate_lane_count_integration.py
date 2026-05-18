from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pandas as pd
from pandas.errors import EmptyDataError

from smart_crosswalk_sumo.preprocess import preprocess_inputs
from smart_crosswalk_sumo.build_networks import build_all_networks
from smart_crosswalk_sumo.collect_metrics import collect_all
from smart_crosswalk_sumo.generate_demand import generate_for_candidates


PROJECT_DIR = Path(__file__).resolve().parents[1]


def remove_lane_count_from_metadata(nets_src: Path, nets_dst: Path) -> None:
    if nets_dst.exists():
        shutil.rmtree(nets_dst)
    shutil.copytree(nets_src, nets_dst)
    for md in nets_dst.glob("cw_*/metadata.json"):
        data = json.loads(md.read_text(encoding="utf-8"))
        data.pop("lane_count_csv", None)
        data.pop("lane_count_source", None)
        data.pop("lane_count_inferred", None)
        md.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def collect_metadata_audit(nets_dir: Path) -> pd.DataFrame:
    rows = []
    for md in sorted(nets_dir.glob("cw_*/metadata.json")):
        data = json.loads(md.read_text(encoding="utf-8"))
        rows.append(
            {
                "crosswalk_id": str(data.get("cw_id", md.parent.name.replace("cw_", ""))),
                "metadata_path": str(md),
                "has_lane_count_csv": "lane_count_csv" in data,
                "has_lane_count_source": "lane_count_source" in data,
                "has_lane_count_inferred": "lane_count_inferred" in data,
                "lane_count_csv": data.get("lane_count_csv"),
                "lane_count_source": data.get("lane_count_source"),
                "lane_count_inferred": data.get("lane_count_inferred"),
            }
        )
    return pd.DataFrame(rows)


def run_validation(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    out_dir = run_dir / "outputs"
    nets_after = run_dir / "sumo_nets_after"
    nets_before = run_dir / "sumo_nets_before"
    out_after = run_dir / "outputs_after"
    out_before = run_dir / "outputs_before"
    run_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_after.mkdir(parents=True, exist_ok=True)
    out_before.mkdir(parents=True, exist_ok=True)

    target_ids = args.target_crosswalk_ids if args.target_crosswalk_ids else None
    candidates, _, _ = preprocess_inputs(args.t1, args.t2, out_dir, args.top_n, target_ids)
    candidates_csv = out_dir / "candidates.csv"

    build_all_networks(
        candidates_csv=candidates_csv,
        nets_dir=nets_after,
        output_dir=out_dir,
        force=True,
        network_radius_m=args.network_radius_m,
        network_mode=args.network_mode,
        admin_polygon_path=args.admin_polygon_path,
        buffer_m=args.buffer_m,
        corridor_whitelist=None,
    )

    generate_for_candidates(
        candidates_csv=candidates_csv,
        nets_dir=nets_after,
        output_dir=out_dir,
        seeds=tuple(args.seeds),
        sim_duration=args.sim_duration,
        warmup=args.warmup,
        step_length=1.0,
        demand_profile="average",
        traffic_counts_csv=None,
        representative_day_id=None,
        model_parameters_path=None,
        vehicle_only=True,
    )

    audit_df = collect_metadata_audit(nets_after)
    audit_path = out_dir / "lane_count_metadata_audit.csv"
    audit_df.to_csv(audit_path, index=False)

    summary_df = (
        audit_df.groupby("lane_count_source", dropna=False).size().reset_index(name="count").rename(columns={"lane_count_source": "source"})
    )
    for label in ["t2_observed", "network_inferred", "fallback_default"]:
        if label not in set(summary_df["source"].fillna("")):
            summary_df = pd.concat([summary_df, pd.DataFrame([{"source": label, "count": 0}])], ignore_index=True)
    summary_df = summary_df.sort_values("source")
    summary_path = out_dir / "lane_count_source_summary.csv"
    summary_df.to_csv(summary_path, index=False)

    fallback_df = audit_df[audit_df["lane_count_source"] == "fallback_default"].copy()
    fallback_path = out_dir / "lane_count_fallback_candidates.csv"
    fallback_df.to_csv(fallback_path, index=False)

    # After run (metadata lane_count_csv used)
    collect_all(
        candidates_csv=candidates_csv,
        output_dir=out_after,
        nets_dir=nets_after,
        sim_duration=args.sim_duration,
        warmup=args.warmup,
        seeds=tuple(args.seeds),
        traci_step_length=args.traci_step_length,
        traffic_measure_radius_m=args.traffic_measure_radius_m,
        vehicle_only=True,
    )

    # Before-like run (remove metadata lane_count keys)
    remove_lane_count_from_metadata(nets_after, nets_before)
    collect_all(
        candidates_csv=candidates_csv,
        output_dir=out_before,
        nets_dir=nets_before,
        sim_duration=args.sim_duration,
        warmup=args.warmup,
        seeds=tuple(args.seeds),
        traci_step_length=args.traci_step_length,
        traffic_measure_radius_m=args.traffic_measure_radius_m,
        vehicle_only=True,
    )

    # Consistency check: after lane_count vs metadata lane_count_csv
    after_seed_path = out_after / "simulation_results_seed.csv"
    if not after_seed_path.exists() or after_seed_path.stat().st_size == 0:
        pd.DataFrame().to_csv(out_dir / "lane_count_metrics_consistency_check.csv", index=False)
        pd.DataFrame().to_csv(out_dir / "lane_count_before_after_comparison.csv", index=False)
        report_path = out_dir / "lane_count_validation_report.md"
        report_path.write_text(
            "# Lane Count Metadata Validation Report\n\n시뮬레이션 결과 파일이 비어 비교를 수행하지 못했습니다. 대상 후보/네트워크 조건을 조정해 재실행이 필요합니다.\n",
            encoding="utf-8",
        )
        print(report_path)
        return
    try:
        after_seed = pd.read_csv(after_seed_path)
    except EmptyDataError:
        after_seed = pd.DataFrame()
    if after_seed.empty:
        pd.DataFrame().to_csv(out_dir / "lane_count_metrics_consistency_check.csv", index=False)
        pd.DataFrame().to_csv(out_dir / "lane_count_before_after_comparison.csv", index=False)
        report_path = out_dir / "lane_count_validation_report.md"
        report_path.write_text(
            "# Lane Count Metadata Validation Report\\n\\n메타데이터 주입 검증은 완료되었으나, vehicle-only 결과 파일이 비어 before/after 비교를 수행하지 못했습니다.\\n",
            encoding="utf-8",
        )
        print(report_path)
        return
    md_map = audit_df[["crosswalk_id", "lane_count_csv", "lane_count_source", "lane_count_inferred"]].copy()
    after_seed["crosswalk_id"] = after_seed["crosswalk_id"].astype(str)
    md_map["crosswalk_id"] = md_map["crosswalk_id"].astype(str)
    merged = after_seed.merge(md_map, on="crosswalk_id", how="left")
    merged["lane_count_match"] = (merged["lane_count"].round(6) == pd.to_numeric(merged["lane_count_csv"], errors="coerce").round(6))
    consistency_cols = [
        "crosswalk_id", "seed", "scenario", "lane_count", "lane_count_csv", "lane_count_source", "lane_count_inferred", "lane_count_match"
    ]
    consistency_df = merged[consistency_cols]
    consistency_path = out_dir / "lane_count_metrics_consistency_check.csv"
    consistency_df.to_csv(consistency_path, index=False)

    # Before/After comparison
    before_seed = pd.read_csv(out_before / "simulation_results_seed.csv")
    before_seed["crosswalk_id"] = before_seed["crosswalk_id"].astype(str)
    key_cols = ["crosswalk_id", "seed", "scenario"]
    cols_keep = [
        "lane_count",
        "vehicle_arrival_rate_per_lane",
        "vehicle_num_lanes",
        "avg_vehicle_delay_sec",
        "avg_waiting_time_sec",
        "avg_queue_length",
        "max_queue_length",
    ]
    before_small = before_seed[key_cols + [c for c in cols_keep if c in before_seed.columns]].copy()
    after_small = after_seed[key_cols + [c for c in cols_keep if c in after_seed.columns]].copy()
    comp = before_small.merge(after_small, on=key_cols, suffixes=("_before", "_after"), how="inner")
    for c in cols_keep:
        b = f"{c}_before"
        a = f"{c}_after"
        if b in comp.columns and a in comp.columns:
            comp[f"delta_{c}"] = pd.to_numeric(comp[a], errors="coerce") - pd.to_numeric(comp[b], errors="coerce")
    comp_path = out_dir / "lane_count_before_after_comparison.csv"
    comp.to_csv(comp_path, index=False)

    invalid_walk_src = out_dir / "invalid_walkingarea_connections.csv"
    invalid_walk_dst = out_dir / "invalid_walkingarea_connections.csv"
    if invalid_walk_src.exists() and invalid_walk_src != invalid_walk_dst:
        shutil.copy2(invalid_walk_src, invalid_walk_dst)
    invalid_ped = out_dir / "invalid_pedestrian_routes.csv"
    if not invalid_ped.exists():
        pd.DataFrame(columns=["crosswalk_id", "seed", "reason", "detail"]).to_csv(invalid_ped, index=False)

    vehicle_only_summary = (
        after_seed.groupby(["crosswalk_id", "scenario"], as_index=False)[
            ["lane_count", "vehicle_arrival_rate_per_lane", "queue_vehicle_num_lanes"]
        ]
        .mean()
    )
    vehicle_only_summary.to_csv(out_dir / "vehicle_only_validation_results.csv", index=False)

    # Markdown report
    total = len(audit_df)
    has_all = int(
        audit_df["has_lane_count_csv"].all()
        and audit_df["has_lane_count_source"].all()
        and audit_df["has_lane_count_inferred"].all()
    )
    summary_map = {str(r["source"]): int(r["count"]) for _, r in summary_df.iterrows()}
    fallback_count = int(summary_map.get("fallback_default", 0))
    fallback_ratio = (fallback_count / total * 100.0) if total else 0.0
    match_rate = float(consistency_df["lane_count_match"].mean() * 100.0) if len(consistency_df) else 0.0

    delta_cols = [c for c in comp.columns if c.startswith("delta_")]
    delta_lines = []
    for dc in delta_cols:
        s = pd.to_numeric(comp[dc], errors="coerce")
        delta_lines.append(f"- {dc}: mean={s.mean():.6f}, min={s.min():.6f}, max={s.max():.6f}")

    report = [
        "# Lane Count Metadata Validation Report",
        "",
        "## 1) Metadata field injection check",
        f"- audited crosswalks: {total}",
        f"- all metadata have lane_count_csv/lane_count_source/lane_count_inferred: {'yes' if has_all else 'no'}",
        "",
        "## 2) lane_count_source summary",
        f"- t2_observed: {summary_map.get('t2_observed', 0)}",
        f"- network_inferred: {summary_map.get('network_inferred', 0)}",
        f"- fallback_default: {summary_map.get('fallback_default', 0)} ({fallback_ratio:.2f}%)",
        "",
        "## 3) fallback candidates",
        f"- saved: {fallback_path}",
        f"- count: {len(fallback_df)}",
        "",
        "## 4) metrics consistency (lane_count vs metadata lane_count_csv)",
        f"- rows checked: {len(consistency_df)}",
        f"- match rate: {match_rate:.2f}%",
        "",
        "## 5) before/after comparison (vehicle-only)",
        f"- comparison rows: {len(comp)}",
        *delta_lines,
        "",
        "## Runtime diagnosis",
        "- invalid walkingarea connection is a network connection entry (not lane_count logic).",
        f"- reproduction command: `sumo -c { (nets_after / 'cw_23040' / 'baseline_seed42.sumocfg') if (nets_after / 'cw_23040' / 'baseline_seed42.sumocfg').exists() else 'N/A' }`",
        "",
        "## Output files",
        f"- {audit_path}",
        f"- {summary_path}",
        f"- {fallback_path}",
        f"- {consistency_path}",
        f"- {comp_path}",
        f"- {out_dir / 'invalid_walkingarea_connections.csv'}",
        f"- {out_dir / 'invalid_pedestrian_routes.csv'}",
        f"- {out_dir / 'vehicle_only_validation_results.csv'}",
    ]

    report_path = out_dir / "lane_count_validation_report.md"
    report_path.write_text("\n".join(report), encoding="utf-8")
    diagnosis_path = out_dir / "sumo_runtime_error_diagnosis.md"
    diagnosis_path.write_text(
        "\n".join(
            [
                "# SUMO Runtime Error Diagnosis",
                "",
                "## Direct Cause",
                "- `network.net.xml` contained invalid connection: `585341903#0 -> :11177413143_w0`.",
                "- `:11177413143_w0` is `function=walkingarea` edge.",
                "- This is a network-generation/connection issue, not caused by lane_count metadata injection.",
                "",
                "## Minimal Reproduction",
                f"- `sumo -c {nets_after / 'cw_23040' / 'baseline_seed42.sumocfg'}`",
                "",
                "## Mitigation Applied",
                "- Post-processing now removes non-pedestrian-lane -> walkingarea invalid connections.",
                "- Validation runs in `vehicle-only` mode to verify lane_count application path end-to-end.",
            ]
        ),
        encoding="utf-8",
    )
    print(report_path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run_dir", required=True)
    ap.add_argument("--t1", default=str(PROJECT_DIR / "smart_crosswalk_sumo" / "data" / "T1_accident_crosswalk.csv"))
    ap.add_argument("--t2", default=str(PROJECT_DIR / "smart_crosswalk_sumo" / "data" / "T2_crosswalk_features.csv"))
    ap.add_argument("--top_n", type=int, default=10)
    ap.add_argument("--network_radius_m", type=float, default=None)
    ap.add_argument("--network_mode", default="expanded")
    ap.add_argument("--admin_polygon_path", default=str(PROJECT_DIR / "smart_crosswalk_sumo" / "data" / "junggu_admin_boundary.geojson"))
    ap.add_argument("--buffer_m", type=float, default=1000.0)
    ap.add_argument("--sim_duration", type=int, default=600)
    ap.add_argument("--warmup", type=int, default=120)
    ap.add_argument("--seeds", nargs="+", type=int, default=[42])
    ap.add_argument("--traci_step_length", type=float, default=0.2)
    ap.add_argument("--traffic_measure_radius_m", type=float, default=500.0)
    ap.add_argument("--target_crosswalk_ids", nargs="*", default=None)
    args = ap.parse_args()
    run_validation(args)
