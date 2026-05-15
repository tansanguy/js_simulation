from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(ROOT := Path(__file__).resolve().parents[1]))

import smart_crosswalk_sumo.phase6_result_standardizer as std


ROOT = Path(__file__).resolve().parents[1]
KOREAN_RE = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")


def _run_module(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "smart_crosswalk_sumo.phase6_result_standardizer", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_csv(path: Path, data: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(data).to_csv(path, index=False)


def contains_korean(text: str) -> bool:
    return bool(KOREAN_RE.search(text))


def assert_no_korean_in_csv(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    assert not contains_korean(text), f"Korean text found in {path}"


def test_help_runs() -> None:
    proc = _run_module("--help", cwd=ROOT)
    assert proc.returncode == 0
    assert "standardize" in proc.stdout
    assert "cleanup" in proc.stdout


def test_standardize_creates_std_layout_and_tolerates_missing_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "human"
    figures = source / "figures"
    figures.mkdir(parents=True)
    (figures / "tradeoff_summary.png").write_bytes(b"PNG")

    _write_csv(
        source / "outputs" / "network_simulation_summary_seed.csv",
        [
            {
                "scenario_group": "integrated_network",
                "scenario": "baseline",
                "seed": 1,
                "safety_risk_score": 11.0,
                "accident_expected_value": 11000000.0,
                "elderly_incomplete_crossings": 2.0,
            },
            {
                "scenario_group": "integrated_network",
                "scenario": "smart_selected",
                "seed": 1,
                "safety_risk_score": 5.0,
                "accident_expected_value": 5000000.0,
                "elderly_incomplete_crossings": 1.0,
            },
        ],
    )
    _write_csv(
        source / "outputs" / "per_crosswalk_simulation_results_seed.csv",
        [
            {
                "crosswalk_id": "NODE_10376",
                "seed": 1,
                "scenario": "baseline",
                "PET_A_proxy_count": 0,
                "PET_B_surrogate_count": 7,
                "PET_B_surrogate_mean": -4.0,
                "safety_risk_score": 11.0,
                "accident_expected_value": 11000000.0,
                "elderly_incomplete_crossings": 2.0,
            },
            {
                "crosswalk_id": "NODE_10376",
                "seed": 1,
                "scenario": "smart_selected",
                "PET_A_proxy_count": 0,
                "PET_B_surrogate_count": 3,
                "PET_B_surrogate_mean": -2.5,
                "safety_risk_score": 5.0,
                "accident_expected_value": 5000000.0,
                "elderly_incomplete_crossings": 1.0,
            },
        ],
    )
    _write_csv(
        source / "outputs" / "baseline_vs_smart_summary.csv",
        [
            {
                "crosswalk_id": "NODE_10376",
                "safety_risk_delta": -6.0,
                "accident_expected_delta": -6000000.0,
                "elderly_incomplete_crossings_delta": -1.0,
            }
        ],
    )
    _write_csv(
        source / "outputs" / "crosswalk_id_mapping.csv",
        [
            {
                "requested_crosswalk_id": "NODE_10376",
                "canonical_crosswalk_id": 10376,
                "registry_crosswalk_id": 10376,
            }
        ],
    )

    proc = _run_module("standardize", "--input-dir", str(source), "--output-dir", str(output), cwd=ROOT)
    assert proc.returncode == 0, proc.stderr

    assert (output / "csv").is_dir()
    assert (output / "readme").is_dir()
    assert (output / "figures").is_dir()
    assert (output / "archive").is_dir()
    assert (output / "README_PHASE6_RESULTS.md").is_file()
    assert (output / "run_metadata.json").is_file()

    for name in std.STANDARD_CSVS:
        assert (output / "csv" / name).is_file()
    for name in std.STANDARD_READMES:
        assert (output / "readme" / name).is_file()

    missing_readme = (output / "readme" / "05_signal_phase_audit_README.md").read_text(encoding="utf-8")
    assert "파일 없음" in missing_readme
    missing_readme_2 = (output / "readme" / "06_impact_scope_edge_counts_README.md").read_text(encoding="utf-8")
    assert "파일 없음" in missing_readme_2

    metadata = json.loads((output / "run_metadata.json").read_text(encoding="utf-8"))
    assert metadata["source_root"] == str(source.resolve())
    assert metadata["source_kind"] in {"smoke_or_integrated_selected", "generic_result_tree"}


def test_inventory_marks_only_existing_columns(tmp_path: Path) -> None:
    src = tmp_path / "result_a"
    _write_csv(
        src / "outputs" / "per_crosswalk_simulation_results_seed.csv",
        [
            {
                "crosswalk_id": "NODE_10376",
                "PET_A_proxy_count": 0,
                "PET_B_surrogate_count": 7,
                "PET_B_surrogate_mean": -4.0,
                "safety_risk_score": 11.0,
                "accident_expected_value": 11000000.0,
                "elderly_incomplete_crossings": 2.0,
            }
        ],
    )
    _write_csv(
        src / "outputs" / "baseline_vs_smart_summary.csv",
        [
            {
                "crosswalk_id": "NODE_10376",
                "safety_risk_delta": -6.0,
                "accident_expected_delta": -6000000.0,
                "elderly_incomplete_crossings_delta": -1.0,
            }
        ],
    )

    inv = std.build_safety_proxy_metric_inventory([src])
    hit = inv[inv["exists"] == True]  # noqa: E712
    assert set(hit["csv_file"].unique()) == {
        "outputs/baseline_vs_smart_summary.csv",
        "outputs/per_crosswalk_simulation_results_seed.csv",
    }
    pet = hit[(hit["csv_file"] == "outputs/per_crosswalk_simulation_results_seed.csv") & (hit["metric_column"] == "PET_A_proxy_count")]
    assert int(pet.iloc[0]["non_null_count"]) == 1
    assert "surrogate" in str(pet.iloc[0]["interpretation"])
    acc = hit[(hit["csv_file"] == "outputs/per_crosswalk_simulation_results_seed.csv") & (hit["metric_column"] == "accident_expected_value")]
    assert "actual accident count" in str(acc.iloc[0]["caution"])


def test_cleanup_defaults_to_no_delete_and_skips_protected_files(tmp_path: Path) -> None:
    result_root = tmp_path / "result"
    protected = result_root / "phase6_vehicle_experiment_300_30seed_human"
    (protected / "csv").mkdir(parents=True)
    (protected / "readme").mkdir(parents=True)
    (protected / "figures").mkdir(parents=True)
    (protected / "archive").mkdir(parents=True)
    (protected / "README_PHASE6_RESULTS.md").write_text("root readme", encoding="utf-8")
    (protected / "run_metadata.json").write_text("{}", encoding="utf-8")
    _write_csv(protected / "csv" / "00_run_completion_summary.csv", [{"seed": 1}])
    (protected / "csv" / "00_run_completion_summary_README.md").write_text("readme", encoding="utf-8")

    junk = result_root / "cw_NODE_10376_smart_s1_mapping_test" / "outputs"
    junk.mkdir(parents=True)
    (junk / ".DS_Store").write_bytes(b"")
    (junk / "edge_data_seed1.xml").write_text("<xml/>", encoding="utf-8")
    (junk / "lane_data_seed1.xml").write_text("<xml/>", encoding="utf-8")
    (junk / "runtime_trace_seed1.log").write_text("trace", encoding="utf-8")
    (junk / "sumo_stdout_seed1.log").write_text("", encoding="utf-8")
    (junk / "sumo_stderr_seed1.log").write_text("", encoding="utf-8")
    (junk / "calibration_report.csv").write_text("", encoding="utf-8")
    (junk / "calibration_summary.md").write_text("", encoding="utf-8")

    out_csv = tmp_path / "cleanup.csv"
    proc = _run_module("cleanup", "--result-root", str(result_root), "--output-csv", str(out_csv), cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["deleted"] == 0
    assert payload["apply_cleanup"] is False
    assert out_csv.is_file()

    df = pd.read_csv(out_csv)
    assert "phase6_vehicle_experiment_300_30seed_human/csv/00_run_completion_summary.csv" not in set(df["file_path"])
    assert "phase6_vehicle_experiment_300_30seed_human/README_PHASE6_RESULTS.md" not in set(df["file_path"])
    assert df[df["safe_to_delete"] == True].shape[0] > 0  # noqa: E712
    assert "cw_NODE_10376_smart_s1_mapping_test/outputs/edge_data_seed1.xml" in set(df["file_path"])


def test_phase6_existing_folder_audit_marks_current_layout(tmp_path: Path) -> None:
    folder = tmp_path / "phase6_vehicle_experiment_300_30seed_human"
    _write_csv(folder / "csv" / "00_run_completion_summary.csv", [{"seed": 1}])
    audit = std.build_phase6_standard_folder_audit(folder)
    row_map = {row["expected_name"]: row for row in audit.to_dict(orient="records")}
    assert row_map["csv"]["exists"] is True
    assert row_map["readme"]["exists"] is False
    assert row_map["README_PHASE6_RESULTS.md"]["exists"] is False
    assert row_map["run_metadata.json"]["exists"] is False
    assert row_map["figures"]["exists"] is False
    assert row_map["archive"]["exists"] is False
    assert sum(1 for row in audit.to_dict(orient="records") if row["item_type"] == "csv" and row["exists"]) == 1
    assert sum(1 for row in audit.to_dict(orient="records") if row["item_type"] == "csv_readme" and row["exists"]) == 0


def test_merge_30seed_results_builds_standard_human_pack(tmp_path: Path) -> None:
    root = tmp_path / "phase_next_top33_recovery"
    output = tmp_path / "phase_next_top33_recovery_human"

    for seed in [1, 2]:
        for scenario in ["baseline", "smart"]:
            source = root / f"phase6_seed{seed}_{scenario}"
            _write_csv(
                source / "network_simulation_summary_seed.csv",
                [
                    {
                        "scenario_group": "integrated_network",
                        "scenario": scenario,
                        "seed": seed,
                        "extension_count": 0 if scenario == "baseline" else seed,
                        "total_extension_sec": 0 if scenario == "baseline" else seed * 5,
                        "network_arrived_vehicles": 3,
                        "avg_vehicle_delay_sec": 15.0 + seed,
                        "avg_queue_length": 0.3 + seed / 100.0,
                        "max_queue_length": 2.0,
                        "network_avg_travel_time_sec": 263.7,
                        "safety_risk_score": 11.0 if scenario == "baseline" else 5.0,
                        "accident_expected_value": 11000000.0 if scenario == "baseline" else 5000000.0,
                        "elderly_incomplete_crossings": 2.0 if scenario == "baseline" else 1.0,
                    }
                ],
            )
            _write_csv(
                source / "per_crosswalk_simulation_results_seed.csv",
                [
                    {
                        "crosswalk_id": "NODE_10376",
                        "source_crosswalk_id": "NODE_10376",
                        "canonical_crosswalk_id": 10376,
                        "registry_crosswalk_id": 10376,
                        "tls_id": 11139418706,
                        "ped_link_index": 2,
                        "scenario": scenario,
                        "seed": seed,
                        "extension_count": 0 if scenario == "baseline" else seed,
                        "total_extension_sec": 0 if scenario == "baseline" else seed * 5,
                        "avg_ped_wait_sec": 50.0 if scenario == "baseline" else 40.0,
                        "avg_vehicle_delay_sec": 0.0,
                        "avg_queue_length": 0.0,
                        "max_queue_length": 0.0,
                        "network_avg_travel_time_sec": 263.7,
                        "PET_A_proxy_count": 0,
                        "PET_A_proxy_mean": 0.0,
                        "PET_A_proxy_severe": 0,
                        "PET_A_proxy_moderate": 0,
                        "PET_A_proxy_safe": 0,
                        "PET_B_surrogate_count": 7 if scenario == "baseline" else 3,
                        "PET_B_surrogate_mean": -4.0 if scenario == "baseline" else -2.5,
                        "PET_B_surrogate_severe": 7 if scenario == "baseline" else 3,
                        "PET_B_surrogate_moderate": 0,
                        "PET_B_surrogate_safe": 0,
                        "safety_risk_score": 11.0 if scenario == "baseline" else 5.0,
                        "accident_expected_value": 11000000.0 if scenario == "baseline" else 5000000.0,
                        "elderly_incomplete_crossings": 2.0 if scenario == "baseline" else 1.0,
                    }
                ],
            )
            _write_csv(
                source / "signal_phase_audit.csv",
                [
                    {
                        "crosswalk_id": "NODE_10376",
                        "tls_id": 11139418706,
                        "ped_link_index": 2,
                        "ped_has_green": True,
                        "ped_has_red": True,
                        "pedestrian_only_phase_available": False,
                        "mixed_phase_only": True,
                        "phase_extension_affects_non_ped_green": True,
                    }
                ],
            )
            _write_csv(
                source / "impact_scope_edge_counts.csv",
                [
                    {
                        "seed": seed,
                        "scenario": scenario,
                        "crosswalk_id": "NODE_10376",
                        "scope": "100m",
                        "unique_edge_count": 10 + seed,
                        "impact_edge_source_main": "radius_geometry",
                        "source_impact_edges_csv": "impact_edges.csv",
                    }
                ],
            )
            if scenario == "smart":
                _write_csv(
                    source / "extension_events_seed.csv",
                    [
                        {
                            "sim_time": 100.0 + seed,
                            "crosswalk_id": "NODE_10376",
                            "tls_id": 11139418706,
                            "ped_link_index": 2,
                            "state": "rrGrr",
                            "phase": 1,
                            "extension_sec": 5.0,
                            "ped_count_on_crossing": 4,
                            "seed": seed,
                            "scenario": "smart",
                        }
                    ],
                )
            elif seed == 1:
                _write_csv(
                    source / "extension_events_seed.csv",
                    [
                        {
                            "sim_time": 99.0,
                            "crosswalk_id": "NODE_10376",
                            "tls_id": 11139418706,
                            "ped_link_index": 2,
                            "state": "rrGrr",
                            "phase": 1,
                            "extension_sec": 5.0,
                            "ped_count_on_crossing": 2,
                            "seed": seed,
                            "scenario": "baseline",
                        }
                    ],
                )

    proc = _run_module(
        "merge-30seed",
        "--root-dir",
        str(root),
        "--baseline-pattern",
        "phase6_seed{seed}_baseline",
        "--smart-pattern",
        "phase6_seed{seed}_smart",
        "--seeds",
        "1-2",
        "--output-dir",
        str(output),
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr

    for name in std.STANDARD_CSVS:
        assert (output / "csv" / name).is_file()
    for name in std.STANDARD_READMES:
        assert (output / "readme" / name).is_file()
    assert (output / "README_PHASE6_RESULTS.md").is_file()
    assert (output / "run_metadata.json").is_file()
    scope_audit = pd.read_csv(output / "csv" / "phase6_extension_scope_audit.csv")
    assert scope_audit.shape[0] == 3
    assert set(scope_audit["scenario"].astype(str)) == {"baseline", "smart"}
    smart_scope = scope_audit[scope_audit["scenario"].astype(str) == "smart"]
    assert int((smart_scope["phase_extension_affects_non_ped_green"] == False).sum()) == 2  # noqa: E712
    assert int((smart_scope["ped_link_is_green"] == True).sum()) == 2  # noqa: E712
    baseline_scope = scope_audit[scope_audit["scenario"].astype(str) == "baseline"]
    assert int((baseline_scope["baseline_extension_event_error"] == True).sum()) == 1  # noqa: E712

    long_df = pd.read_csv(output / "csv" / "01_all_results_long.csv")
    assert set(long_df["seed"].astype(int)) == {1, 2}
    assert set(long_df["scenario"].astype(str)) == {"baseline", "smart"}
    assert "PET_A_proxy_count" in long_df.columns
    assert "accident_expected_value" in long_df.columns

    delta_df = pd.read_csv(output / "csv" / "02_baseline_smart_paired_delta.csv")
    assert "safety_risk_score_delta" in delta_df.columns
    assert "accident_expected_delta" in delta_df.columns
    assert "elderly_incomplete_crossings_delta" in delta_df.columns
    assert delta_df.shape[0] == 2

    readme = (output / "README_PHASE6_RESULTS.md").read_text(encoding="utf-8")
    assert "30 seed paired baseline/smart 결과" in readme
    assert "accident_expected_value는 실제 사고 건수가 아닌 사고 위험 proxy다" in readme
    assert "mixed phase" in readme
    assert "smart extension event all pedestrian-only: yes" in readme


def test_failed_smoke_standardize_writes_readable_header_only_csvs_and_english_inventory(tmp_path: Path) -> None:
    source = tmp_path / "failed_smoke"
    output = tmp_path / "failed_smoke_human"
    source.mkdir(parents=True)
    (source / "runtime_trace_seed1.log").write_text("trace", encoding="utf-8")
    (source / "failed.sumocfg").write_text("<configuration/>", encoding="utf-8")
    (source / "generated.rou.xml").write_text("<routes/>", encoding="utf-8")
    (source / "phase6_smoke_summary.md").write_text("# smoke", encoding="utf-8")

    proc = _run_module("standardize", "--input-dir", str(source), "--output-dir", str(output), cwd=ROOT)
    assert proc.returncode == 0, proc.stderr

    targets = [
        "debug/route_generation_audit.csv",
        "debug/pedestrian_route_connectivity_audit.csv",
        "debug/signal_phase_audit.csv",
        "debug/skipped_pedestrian_routes.csv",
        "debug/source_file_inventory.csv",
        "results/00_run_completion_summary.csv",
        "results/01_global_traffic_results_long.csv",
        "results/02_target_crosswalk_safety_results_long.csv",
        "results/03_baseline_smart_paired_delta.csv",
        "results/04_summary_by_crosswalk.csv",
        "results/05_extension_events_all.csv",
        "phase6_extension_scope_audit.csv",
        "results/06_benefit_input_table.csv",
        "safety/safety_metrics_by_run.csv",
        "safety/safety_paired_delta.csv",
        "safety/safety_summary_by_crosswalk.csv",
    ]
    for rel in targets:
        path = output / "csv" / rel
        assert path.is_file()
        df = pd.read_csv(path)
        assert isinstance(df, pd.DataFrame)
        assert_no_korean_in_csv(path)

    skipped = pd.read_csv(output / "csv" / "debug" / "skipped_pedestrian_routes.csv")
    assert list(skipped.columns) == std.STANDARD_CSV_SCHEMAS["skipped_pedestrian_routes.csv"]

    inv = pd.read_csv(output / "csv" / "debug" / "source_file_inventory.csv")
    assert set(inv["file_name"].astype(str)) >= {
        "runtime_trace_seed1.log",
        "failed.sumocfg",
        "generated.rou.xml",
        "phase6_smoke_summary.md",
    }
    assert set(inv["recognized_as_input"].astype(str)) <= {"False", "true", "false"}
    assert all(str(v).lower() == "false" for v in inv["recognized_as_input"].tolist())
    for col in ["reason", "note"]:
        assert all(not contains_korean(str(v)) for v in inv[col].astype(str).tolist())

    readme = (output / "README_PHASE6_RESULTS.md").read_text(encoding="utf-8")
    assert "CSV files are intentionally English-only" in readme
    assert "Header-only CSVs mean the smoke output was failed or incomplete" in readme


def test_standardize_writes_scoped_debug_results_and_safety_tables(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "human_scoped"

    _write_csv(
        source / "outputs" / "network_simulation_summary_seed.csv",
        [
            {
                "scenario_group": "integrated_network",
                "scenario": "baseline",
                "seed": 7,
                "network_arrived_vehicles": 120,
                "network_avg_travel_time_sec": 252.5,
                "avg_vehicle_delay_sec": 18.0,
                "avg_queue_length": 0.7,
                "max_queue_length": 4.0,
                "total_vehicle_delay": 3600.0,
                "extension_count": 0,
                "total_extension_sec": 0.0,
                "simulation_duration_sec": 1800,
                "demand_multiplier": 1.0,
                "install_cost_krw": 25000000,
            },
            {
                "scenario_group": "integrated_network",
                "scenario": "smart",
                "seed": 7,
                "network_arrived_vehicles": 118,
                "network_avg_travel_time_sec": 251.0,
                "avg_vehicle_delay_sec": 16.5,
                "avg_queue_length": 0.6,
                "max_queue_length": 3.5,
                "total_vehicle_delay": 3300.0,
                "extension_count": 1,
                "total_extension_sec": 5.0,
                "simulation_duration_sec": 1800,
                "demand_multiplier": 1.0,
                "install_cost_krw": 25000000,
            },
        ],
    )
    _write_csv(
        source / "outputs" / "per_crosswalk_simulation_results_seed.csv",
        [
            {
                "crosswalk_id": "CW_1",
                "seed": 7,
                "scenario": "baseline",
                "avg_ped_wait_sec": 48.0,
                "pedestrian_count_near_target": 10,
                "crossing_attempt_count": 10,
                "completed_crossing_count": 8,
                "incomplete_crossing_count": 2,
                "elderly_incomplete_crossings": 1.0,
                "high_risk_event_count": 4,
                "PET_A_proxy_count": 8,
                "PET_A_proxy_mean": 2.4,
                "PET_A_proxy_min": 1.8,
                "PET_A_proxy_severe": 1,
                "PET_A_proxy_moderate": 3,
                "PET_A_proxy_safe": 4,
                "PET_B_surrogate_count": 5,
                "PET_B_surrogate_mean": 1.6,
                "PET_B_surrogate_min": 1.1,
                "PET_B_surrogate_severe": 2,
                "PET_B_surrogate_moderate": 2,
                "PET_B_surrogate_safe": 1,
                "safety_risk_score": 9.0,
                "accident_expected_value": 9000000.0,
                "network_arrived_vehicles": 120,
                "network_avg_travel_time_sec": 252.5,
                "avg_vehicle_delay_sec": 18.0,
                "avg_queue_length": 0.7,
                "max_queue_length": 4.0,
                "total_vehicle_delay_sec": 3600.0,
                "total_extension_count": 0,
                "total_extension_sec": 0.0,
            },
            {
                "crosswalk_id": "CW_1",
                "seed": 7,
                "scenario": "smart",
                "avg_ped_wait_sec": 41.0,
                "pedestrian_count_near_target": 10,
                "crossing_attempt_count": 10,
                "completed_crossing_count": 9,
                "incomplete_crossing_count": 1,
                "elderly_incomplete_crossings": 0.0,
                "high_risk_event_count": 2,
                "PET_A_proxy_count": 9,
                "PET_A_proxy_mean": 2.8,
                "PET_A_proxy_min": 2.0,
                "PET_A_proxy_severe": 0,
                "PET_A_proxy_moderate": 2,
                "PET_A_proxy_safe": 7,
                "PET_B_surrogate_count": 4,
                "PET_B_surrogate_mean": 2.1,
                "PET_B_surrogate_min": 1.4,
                "PET_B_surrogate_severe": 1,
                "PET_B_surrogate_moderate": 1,
                "PET_B_surrogate_safe": 2,
                "safety_risk_score": 5.0,
                "accident_expected_value": 5000000.0,
                "network_arrived_vehicles": 118,
                "network_avg_travel_time_sec": 251.0,
                "avg_vehicle_delay_sec": 16.5,
                "avg_queue_length": 0.6,
                "max_queue_length": 3.5,
                "total_vehicle_delay_sec": 3300.0,
                "total_extension_count": 1,
                "total_extension_sec": 5.0,
            },
        ],
    )
    _write_csv(
        source / "outputs" / "baseline_vs_smart_summary.csv",
        [
            {
                "crosswalk_id": "CW_1",
                "safety_risk_delta": -4.0,
                "accident_expected_delta": -4000000.0,
                "elderly_incomplete_crossings_delta": -1.0,
            }
        ],
    )
    _write_csv(
        source / "outputs" / "crosswalk_id_mapping.csv",
        [
            {
                "requested_crosswalk_id": "CW_1",
                "canonical_crosswalk_id": 1,
                "registry_crosswalk_id": 1,
                "tls_id": "TLS_1",
                "ped_link_index": 2,
            }
        ],
    )

    proc = _run_module("standardize", "--input-dir", str(source), "--output-dir", str(output), cwd=ROOT)
    assert proc.returncode == 0, proc.stderr

    for folder in ["debug", "results", "safety"]:
        assert (output / "csv" / folder).is_dir()

    assert (output / "csv" / "results" / "01_global_traffic_results_long.csv").is_file()
    assert (output / "csv" / "results" / "02_target_crosswalk_safety_results_long.csv").is_file()
    assert (output / "csv" / "results" / "06_benefit_input_table.csv").is_file()
    assert (output / "csv" / "safety" / "safety_metrics_by_run.csv").is_file()
    assert (output / "csv" / "safety" / "safety_paired_delta.csv").is_file()
    assert (output / "csv" / "debug" / "route_generation_audit.csv").is_file()
    assert (output / "csv" / "debug" / "pedestrian_route_connectivity_audit.csv").is_file()
    assert (output / "csv" / "debug" / "signal_phase_audit.csv").is_file()
    assert (output / "csv" / "debug" / "skipped_pedestrian_routes.csv").is_file()
    assert (output / "csv" / "debug" / "source_file_inventory.csv").is_file()

    traffic = pd.read_csv(output / "csv" / "results" / "01_global_traffic_results_long.csv")
    assert set(traffic["traffic_scope"].astype(str)) == {"global_network"}

    safety = pd.read_csv(output / "csv" / "results" / "02_target_crosswalk_safety_results_long.csv")
    assert set(safety["safety_scope"].astype(str)) == {"target_crosswalk_only"}

    safety_metrics = pd.read_csv(output / "csv" / "safety" / "safety_metrics_by_run.csv")
    assert "safety_scope" in safety_metrics.columns
    assert safety_metrics.shape[0] == 2

    safety_delta = pd.read_csv(output / "csv" / "safety" / "safety_paired_delta.csv")
    row = safety_delta.iloc[0]
    assert row["high_risk_event_count_delta"] == -2
    assert row["PET_A_proxy_mean_delta"] == pytest.approx(0.4)
    assert row["safety_risk_score_delta"] == -4.0

    benefit = pd.read_csv(output / "csv" / "results" / "06_benefit_input_table.csv")
    assert benefit.shape[0] == 1
    assert bool(benefit.iloc[0]["usable_for_benefit_analysis"]) is True

    readme = (output / "README_PHASE6_RESULTS.md").read_text(encoding="utf-8")
    assert "traffic_scope = `global_network`" in readme
    assert "safety_scope = `target_crosswalk_only`" in readme
    assert "actual accident count" not in readme
    assert "accident_expected_value는 실제 사고 건수다" not in readme
