from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def test_csv_pipeline_audit_builds_expected_tables(tmp_path: Path) -> None:
    result_root = tmp_path / "result"
    seed30_root = result_root / "phase_next_top50_tiered_recovery_audit" / "seed30" / "NODE_1"
    baseline = seed30_root / "baseline_seed1"
    smart = seed30_root / "smart_seed1"

    _write_csv(
        baseline / "phase6_smoke_baseline_results.csv",
        [
            {
                "crosswalk_id": "NODE_1",
                "scenario": "baseline",
                "seed": 1,
                "completed": True,
                "output_dir": str(baseline),
                "run_name": "phase6_smoke_baseline_seed1",
                "elapsed_sec": 12.5,
                "ped_crossing_person_count": 5,
                "ped_crossing_presence_steps": 10,
                "extension_count": 0,
                "veh_delay_mean": 3.5,
            }
        ],
    )
    _write_csv(
        smart / "phase6_smoke_smart_results.csv",
        [
            {
                "crosswalk_id": "NODE_1",
                "scenario": "smart",
                "seed": 1,
                "completed": True,
                "output_dir": str(smart),
                "run_name": "phase6_smoke_smart_seed1",
                "elapsed_sec": 11.5,
                "ped_crossing_person_count": 5,
                "ped_crossing_presence_steps": 8,
                "extension_count": 1,
                "veh_delay_mean": 2.5,
            }
        ],
    )
    _write_csv(
        result_root / "check_scoped_csv_layout_v1" / "csv" / "safety" / "safety_metrics_by_run.csv",
        [
            {
                "target_crosswalk_id": "NODE_1",
                "seed": 1,
                "scenario": "baseline",
                "high_risk_event_count": 4,
                "PET_A_proxy_count": 8,
                "PET_B_surrogate_count": 7,
                "safety_risk_score": 9.0,
                "accident_expected_value": 9000000.0,
            }
        ],
    )
    (result_root / "phase_next_top50_final_candidate_screening" / "top50_signal_and_location_combined_screening.csv").parent.mkdir(parents=True, exist_ok=True)
    (result_root / "phase_next_top50_final_candidate_screening" / "top50_signal_and_location_combined_screening.csv").write_text("crosswalk_id\nNODE_1\n", encoding="utf-8")

    output_dir = tmp_path / "audit"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "smart_crosswalk_sumo.reporting.csv_pipeline_audit",
            "--result-root",
            str(result_root),
            "--seed30-root",
            str(result_root / "phase_next_top50_tiered_recovery_audit" / "seed30"),
            "--output-dir",
            str(output_dir),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    assert (output_dir / "README.md").is_file()
    assert (output_dir / "run_manifest.csv").is_file()
    assert (output_dir / "run_completion_summary.csv").is_file()
    assert (output_dir / "command_to_run_next_30seed_batch.sh").is_file()
    assert (output_dir / "csv" / "csv_output_inventory.csv").is_file()
    assert (output_dir / "csv" / "seed30_baseline_smart_comparison.csv").is_file()
    assert (output_dir / "csv" / "seed30_metric_summary.csv").is_file()
    assert (output_dir / "csv" / "safety_surrogate_summary.csv").is_file()
    assert (output_dir / "csv" / "cleanup_candidate_inventory.csv").is_file()

    pair_df = pd.read_csv(output_dir / "csv" / "seed30_baseline_smart_comparison.csv")
    assert pair_df.iloc[0]["comparison_status"] == "ready"
    assert pair_df.iloc[0]["baseline_extension_count"] == 0
    assert pair_df.iloc[0]["smart_extension_count"] == 1

    inventory_df = pd.read_csv(output_dir / "csv" / "csv_output_inventory.csv")
    assert bool(inventory_df["has_elapsed_sec"].any()) is True

    safety_df = pd.read_csv(output_dir / "csv" / "safety_metric_presence_check.csv")
    assert bool(safety_df["has_safety"].any()) is True
