#!/usr/bin/env python3
"""
build_result_csv_scaffolds.py
Creates scaffold CSV files for the 30-seed result structure (H단계).
These are EMPTY TEMPLATES - they will be filled after actual runs.
"""

import csv
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parents[1]
RUNS_DIR = BASE / "result/active/real_30seed_runs"
SUMMARY_DIR = RUNS_DIR / "summary"
SUMMARY_DIR.mkdir(parents=True, exist_ok=True)

NET_GROUPS = {
    "current_main_12": [
        "NODE_10335","NODE_8369","NODE_167173","LINK_239754",
        "NODE_5846","NODE_5831","NODE_10377","NODE_10376",
        "NODE_150723","NODE_125895","NODE_10381","LINK_120139",
    ],
    "signal_fix_9": [
        "LINK_249048","NODE_5837","LINK_10218","LINK_140740",
        "NODE_9634","LINK_194891","NODE_10378","NODE_5681","NODE_10262",
    ],
    "generated_signal_7": [
        "LINK_212169","NODE_8452","NODE_6082","NODE_6123",
        "NODE_8426","NODE_74388","NODE_10273",
    ],
    "p1_p4_recovery_6": [
        "NODE_10060","NODE_122781","NODE_14937","NODE_5647","NODE_6342",
        "NODE_5938",  # P4 approved (NODE_5830 dropped)
    ],
}

# ─── Create per-crosswalk folder scaffolds ────────────────────────────────────
for net_group, crosswalk_ids in NET_GROUPS.items():
    net_dir = RUNS_DIR / net_group
    # baseline dir
    baseline_dir = net_dir / "baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    # baseline_summary scaffold
    bsummary_path = baseline_dir / "baseline_summary.csv"
    if not bsummary_path.exists():
        with open(bsummary_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "net_group","scenario","seed","vehicle_count","vehicle_delay",
                "travel_time","waiting_time","speed","pedestrian_count",
                "pedestrian_crossing_count","extension_count","status"
            ])
            writer.writeheader()

    for cid in crosswalk_ids:
        cw_dir = net_dir / cid
        cw_dir.mkdir(parents=True, exist_ok=True)
        (cw_dir / "logs").mkdir(exist_ok=True)
        (cw_dir / "xml").mkdir(exist_ok=True)

        # Scaffold files
        for fname, cols in [
            ("smart_runs.csv", [
                "net_group","crosswalk_id","scenario","seed",
                "vehicle_count","vehicle_delay","travel_time",
                "waiting_time","speed","pedestrian_count",
                "pedestrian_crossing_count","extension_count","status"
            ]),
            ("baseline_vs_smart_comparison.csv", [
                "net_group","crosswalk_id","metric",
                "baseline_mean","smart_mean","delta","delta_pct","note"
            ]),
            ("traffic_summary.csv", [
                "net_group","crosswalk_id","scenario","seed_count",
                "mean_vehicle_count","mean_vehicle_delay",
                "mean_travel_time","mean_waiting_time","mean_speed","note"
            ]),
            ("safety_summary.csv", [
                "net_group","crosswalk_id","scenario","seed_count",
                "mean_pedestrian_count","mean_pedestrian_crossing_count",
                "mean_extension_count","mean_high_risk_event_count",
                "mean_conflict_count","note"
            ]),
            ("run_manifest.csv", [
                "net_group","crosswalk_id","scenario","seed",
                "run_dir","result_csv","status","started_at","completed_at"
            ]),
        ]:
            fp = cw_dir / fname
            if not fp.exists():
                with open(fp, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=cols)
                    writer.writeheader()

        # README per crosswalk
        readme_path = cw_dir / "README.md"
        if not readme_path.exists():
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(f"# {net_group} / {cid}\n\n")
                f.write(f"- Net group: `{net_group}`\n")
                f.write(f"- Crosswalk ID: `{cid}`\n")
                f.write(f"- Net file: `result/active/nets/{net_group}.net.xml`\n")
                f.write(f"- Candidate CSV: `result/active/csv/{net_group}_candidates.csv`\n\n")
                f.write("## Files\n\n")
                f.write("| File | Description |\n|------|-------------|\n")
                f.write("| `baseline_summary.csv` | 30-seed baseline summary (parent dir) |\n")
                f.write("| `smart_runs.csv` | 30-seed smart run raw results |\n")
                f.write("| `baseline_vs_smart_comparison.csv` | Comparison per metric |\n")
                f.write("| `traffic_summary.csv` | Traffic flow summary (30-seed mean) |\n")
                f.write("| `safety_summary.csv` | Safety surrogate summary (30-seed mean) |\n")
                f.write("| `run_manifest.csv` | Run-by-run status log |\n\n")
                f.write("## Status\n\nNOT YET RUN\n")

print(f"[OK] Per-crosswalk scaffold dirs created")

# ─── Summary-level CSVs ───────────────────────────────────────────────────────

# net_group_summary.csv (aggregate placeholder)
with open(SUMMARY_DIR / "net_group_summary.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=[
        "net_group","crosswalk_count","baseline_runs_planned","smart_runs_planned",
        "total_runs_planned","baseline_runs_done","smart_runs_done","status"
    ])
    writer.writeheader()
    for ng, ids in NET_GROUPS.items():
        n = len(ids)
        writer.writerow({
            "net_group": ng,
            "crosswalk_count": n,
            "baseline_runs_planned": 30,
            "smart_runs_planned": n * 30,
            "total_runs_planned": 30 + n * 30,
            "baseline_runs_done": 0,
            "smart_runs_done": 0,
            "status": "NOT_YET_RUN",
        })
print(f"[OK] summary/net_group_summary.csv")

# crosswalk_30seed_mean_summary.csv
with open(SUMMARY_DIR / "crosswalk_30seed_mean_summary.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=[
        "net_group","crosswalk_id","seed_count",
        "mean_vehicle_count","mean_vehicle_delay","mean_travel_time",
        "mean_waiting_time","mean_speed",
        "mean_pedestrian_count","mean_pedestrian_crossing_count",
        "mean_extension_count","status"
    ])
    writer.writeheader()
print(f"[OK] summary/crosswalk_30seed_mean_summary.csv (empty scaffold)")

# baseline_vs_smart_30seed_mean_comparison.csv
with open(SUMMARY_DIR / "baseline_vs_smart_30seed_mean_comparison.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=[
        "net_group","crosswalk_id","metric",
        "baseline_mean","smart_mean","delta","delta_pct","note"
    ])
    writer.writeheader()
print(f"[OK] summary/baseline_vs_smart_30seed_mean_comparison.csv (empty scaffold)")

# traffic_flow_summary.csv
with open(SUMMARY_DIR / "traffic_flow_summary.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=[
        "net_group","scenario","crosswalk_id","seed_count",
        "mean_vehicle_count","mean_vehicle_delay","mean_travel_time",
        "mean_waiting_time","mean_speed","note"
    ])
    writer.writeheader()
print(f"[OK] summary/traffic_flow_summary.csv (empty scaffold)")

# safety_surrogate_summary.csv
with open(SUMMARY_DIR / "safety_surrogate_summary.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=[
        "net_group","scenario","crosswalk_id","seed_count",
        "mean_pedestrian_count","mean_pedestrian_crossing_count",
        "mean_extension_count","mean_high_risk_event_count",
        "mean_conflict_count","note"
    ])
    writer.writeheader()
print(f"[OK] summary/safety_surrogate_summary.csv (empty scaffold)")

# safety_metric_presence_check.csv
with open(SUMMARY_DIR / "safety_metric_presence_check.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=[
        "metric","collected","source","note"
    ])
    writer.writeheader()
    writer.writerows([
        {"metric":"pedestrian_count","collected":"True","source":"traci pedestrian route","note":"collected via ped route generator"},
        {"metric":"pedestrian_crossing_count","collected":"True","source":"traci pedestrian route","note":"collected via ped route generator"},
        {"metric":"extension_count","collected":"True","source":"smart crosswalk logic","note":"TLS phase extension counter"},
        {"metric":"vehicle_count","collected":"True","source":"traci vehicle detector","note":"collected per step"},
        {"metric":"vehicle_delay","collected":"True","source":"traci vehicle detector","note":"accumulated delay per vehicle"},
        {"metric":"travel_time","collected":"True","source":"traci vehicle detector","note":"avg travel time"},
        {"metric":"waiting_time","collected":"True","source":"traci vehicle detector","note":"accumulated wait per vehicle"},
        {"metric":"mean_speed","collected":"True","source":"traci vehicle detector","note":"avg speed per step"},
        {"metric":"high_risk_event_count","collected":"False","source":"not_implemented","note":"currently unavailable / not collected yet - needs risk event collector"},
        {"metric":"conflict_count","collected":"False","source":"not_implemented","note":"currently unavailable / not collected yet - needs conflict detection logic"},
    ])
print(f"[OK] summary/safety_metric_presence_check.csv")

# csv_output_inventory.csv
with open(SUMMARY_DIR / "csv_output_inventory.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=[
        "csv_file","purpose","status","note"
    ])
    writer.writeheader()
    writer.writerows([
        {"csv_file":"net_group_summary.csv","purpose":"per-net run plan and status","status":"SCAFFOLD","note":""},
        {"csv_file":"crosswalk_30seed_mean_summary.csv","purpose":"per-crosswalk 30-seed mean","status":"SCAFFOLD","note":"fill after runs"},
        {"csv_file":"baseline_vs_smart_30seed_mean_comparison.csv","purpose":"baseline vs smart delta","status":"SCAFFOLD","note":"fill after runs"},
        {"csv_file":"traffic_flow_summary.csv","purpose":"traffic flow metrics","status":"SCAFFOLD","note":"fill after runs"},
        {"csv_file":"safety_surrogate_summary.csv","purpose":"safety surrogate metrics","status":"SCAFFOLD","note":"fill after runs; high_risk/conflict not yet collected"},
        {"csv_file":"safety_metric_presence_check.csv","purpose":"which safety metrics are collected","status":"READY","note":""},
        {"csv_file":"csv_output_inventory.csv","purpose":"this file","status":"READY","note":""},
    ])
print(f"[OK] summary/csv_output_inventory.csv")

print(f"\n=== All scaffold CSVs created in {SUMMARY_DIR} ===")
