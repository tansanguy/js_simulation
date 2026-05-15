from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
INPUT_ROOT = ROOT / "result" / "phase_next_top50_max_installation_recovery_20260516_002620"


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def test_prepare_builds_pipeline_pack_without_running_sumo(tmp_path: Path) -> None:
    result_root = tmp_path / "result"
    (result_root / "old_smoke").mkdir(parents=True)
    _write_csv(
        result_root / "old_smoke" / "sample.csv",
        [
            {
                "run_group": "sample_group",
                "net_group": "sample_net",
                "scenario": "baseline",
                "seed": 1,
                "crosswalk_id": "NODE_1",
                "elapsed_sec": 1.25,
                "veh_delay_mean": 3.5,
                "ped_crossing_person_count": 8,
            }
        ],
    )
    (result_root / "old_smoke" / "runtime_trace_seed1.log").write_text("trace", encoding="utf-8")
    (result_root / "old_smoke" / "edge_data_seed1.xml").write_text("<xml />", encoding="utf-8")

    output_root = result_root / "phase_next_30seed_28_ready_pipeline_test"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "smart_crosswalk_sumo.reporting.aggregate_30seed_results",
            "prepare",
            "--input-root",
            str(INPUT_ROOT),
            "--result-root",
            str(result_root),
            "--output-root",
            str(output_root),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    manifest = pd.read_csv(output_root / "run_manifest.csv")
    baseline_manifest = pd.read_csv(output_root / "manifests" / "baseline_run_manifest.csv")
    smart_manifest = pd.read_csv(output_root / "manifests" / "smart_run_manifest.csv")

    assert manifest.shape[0] == 930
    assert baseline_manifest.shape[0] == 90
    assert smart_manifest.shape[0] == 840
    assert (output_root / "commands" / "command_to_run_30seed_all_groups.sh").is_file()
    assert (output_root / "commands" / "command_to_cleanup_result_dry_run.sh").is_file()
    assert (output_root / "commands" / "command_to_cleanup_result_apply.sh").is_file()
    assert (output_root / "csv" / "csv_output_inventory.csv").is_file()
    assert (output_root / "csv" / "csv_pipeline_audit_summary.csv").is_file()
    assert (output_root / "csv" / "safety_metric_presence_check.csv").is_file()
    assert (output_root / "csv" / "result_cleanup_inventory.csv").is_file()
    assert (output_root / "csv" / "result_cleanup_safe_to_delete.csv").is_file()
    assert (output_root / "csv" / "result_cleanup_review_needed.csv").is_file()
    assert (output_root / "csv" / "result_cleanup_protected_files.csv").is_file()
    assert (output_root / "readme" / "result_cleanup_summary.md").is_file()
    assert (output_root / "README.md").is_file()

    cleanup = pd.read_csv(output_root / "csv" / "result_cleanup_inventory.csv")
    assert "old_smoke/sample.csv" in set(cleanup["file_path"].astype(str))
    assert (cleanup["file_path"].astype(str).str.startswith("phase_next_30seed_28_ready_pipeline_test")).sum() == 0

