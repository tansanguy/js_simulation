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
    validation_manifest = pd.read_csv(output_root / "manifests" / "run_validation_manifest.csv")
    validation_summary = pd.read_csv(output_root / "csv" / "run_validation_summary.csv")

    assert manifest.shape[0] == 1140
    assert baseline_manifest.shape[0] == 120
    assert smart_manifest.shape[0] == 1020
    assert validation_manifest.shape[0] >= 10
    assert validation_summary.shape[0] == validation_manifest.shape[0]
    assert "validation_manifest" in manifest.columns
    assert (output_root / "commands" / "command_to_run_30seed_all_groups.sh").is_file()
    assert (output_root / "commands" / "command_to_cleanup_result_dry_run.sh").is_file()
    assert (output_root / "commands" / "command_to_cleanup_result_apply.sh").is_file()
    assert (output_root / "csv" / "csv_output_inventory.csv").is_file()
    assert (output_root / "csv" / "csv_pipeline_audit_summary.csv").is_file()
    assert (output_root / "csv" / "run_validation_summary.csv").is_file()
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


def test_prepare_sampled10_builds_new_root(tmp_path: Path) -> None:
    result_root = tmp_path / "result"
    output_root = result_root / "active" / "real_30seed_runs_sampled10"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "smart_crosswalk_sumo.reporting.aggregate_30seed_results",
            "prepare-sampled10",
            "--input-root",
            str(INPUT_ROOT),
            "--result-root",
            str(result_root),
            "--output-root",
            str(output_root),
            "--active-root",
            str(ROOT / "result" / "active"),
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
    all_commands = manifest["command"].astype(str)

    assert manifest.shape[0] == 1140
    assert baseline_manifest.shape[0] == 120
    assert smart_manifest.shape[0] == 1020
    assert manifest["scenario"].eq("baseline").sum() == 120
    assert manifest["scenario"].eq("smart").sum() == 1020
    assert manifest["seed"].min() == 1
    assert manifest["seed"].max() == 30
    assert smart_manifest["crosswalk_id"].astype(str).nunique() == 34
    assert baseline_manifest["crosswalk_id"].astype(str).nunique() == 4
    assert all_commands.str.contains("--metric-sample-interval 10").sum() == 1140
    assert all_commands.str.contains("--vehicle-sample-interval 10").sum() == 1140
    assert all_commands.str.contains("--progress-interval 60").sum() == 1140
    assert all_commands.str.contains("--skip_reports").sum() == 0
    assert not all_commands.str.contains("phase6_smoke_summary.csv").any()
    assert manifest["expected_summary_csv"].astype(str).str.contains("phase6_smoke_summary.csv").sum() == 0
    assert (output_root / "commands" / "command_to_run_30seed_all_groups.sh").is_file()

    sampled10_script = (output_root / "commands" / "command_to_run_seed1_current_main_12.sh").read_text(encoding="utf-8")
    assert 'export PATH="$SUMO_HOME/bin:$PATH"' in sampled10_script
    assert 'export PATH="$PROJECT_ROOT/.venv/bin:$PATH"' in sampled10_script
    assert sampled10_script.index('export PATH="$PROJECT_ROOT/.venv/bin:$PATH"') > sampled10_script.index('export PATH="$SUMO_HOME/bin:$PATH"')
    assert 'export PATH="$SUMO_HOME/bin:$PATH"\n  if [[ -z "${PROJ_LIB:-}"' in sampled10_script
    assert 'export PATH="$SUMO_HOME/bin:$PATH"' not in sampled10_script.split('if [[ -d "$PROJECT_ROOT/.venv/bin" ]]; then', 1)[-1]

    status_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "smart_crosswalk_sumo.reporting.aggregate_30seed_results",
            "status",
            "--pipeline-root",
            str(output_root),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert status_proc.returncode == 0, status_proc.stderr

    aggregate_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "smart_crosswalk_sumo.reporting.aggregate_30seed_results",
            "aggregate",
            "--pipeline-root",
            str(output_root),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert aggregate_proc.returncode == 0, aggregate_proc.stderr
