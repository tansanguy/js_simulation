from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import datetime
from pathlib import Path

try:
    from .demand_scenarios import DEFAULT_DEMAND_SCENARIO_NAME
    from .model_config import load_model_parameters as load_model_parameters_file
    from .mpl_runtime import ensure_matplotlib_env
except ImportError:
    from demand_scenarios import DEFAULT_DEMAND_SCENARIO_NAME
    from model_config import load_model_parameters as load_model_parameters_file
    from mpl_runtime import ensure_matplotlib_env

ensure_matplotlib_env()

try:
    from .build_networks import build_all_networks
    from .calibration import calibrate
    from .collect_metrics import collect_all
    from .csv_outputs import ensure_csv_output_layout, mirror_existing_csv_files
    from .demand_validation import validate_demand_run
    from .generate_demand import generate_for_candidates
    from .generate_reports import generate_all_reports
    from .integrated_mode import (
        build_integrated_network_manifest,
        collect_integrated_metrics,
        generate_integrated_demand,
        generate_integrated_reports,
        select_smart_crosswalks,
        check_valid_smart_crosswalks,
    )
    from .implementation_diagnostics import generate_implementation_diagnostics
    from .c_recovery_pipeline import generate_c_recovery_reports
    from .model_config import dump_parameter_table
    from .preprocess import preprocess_inputs
    from .visualization_exports import export_visual_assets
except ImportError:
    from build_networks import build_all_networks
    from calibration import calibrate
    from collect_metrics import collect_all
    from csv_outputs import ensure_csv_output_layout, mirror_existing_csv_files
    from demand_validation import validate_demand_run
    from generate_demand import generate_for_candidates
    from generate_reports import generate_all_reports
    from integrated_mode import (
        build_integrated_network_manifest,
        collect_integrated_metrics,
        generate_integrated_demand,
        generate_integrated_reports,
        select_smart_crosswalks,
        check_valid_smart_crosswalks,
    )
    from implementation_diagnostics import generate_implementation_diagnostics
    from c_recovery_pipeline import generate_c_recovery_reports
    from model_config import dump_parameter_table
    from preprocess import preprocess_inputs
    from visualization_exports import export_visual_assets


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
DEFAULT_RESULT_ROOT = PROJECT_DIR / "result"
DEFAULT_JUNGGU_ADMIN_POLYGON_PATH = BASE_DIR / "data" / "junggu_admin_boundary.geojson"


def default_run_name() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def resolve_run_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    run_dir = Path(args.run_dir) if args.run_dir else Path(args.result_root) / (args.run_name or default_run_name())
    output_dir = Path(args.output_dir) if args.output_dir else run_dir / "outputs"
    nets_dir = Path(args.nets_dir) if args.nets_dir else run_dir / "sumo_nets"
    figures_dir = Path(args.figures_dir) if args.figures_dir else run_dir / "figures"
    return run_dir, output_dir, nets_dir, figures_dir


def _is_regeneration_mode(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "skip_run", False))


def _load_json_if_exists(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _has_original_execution_metadata(run_dir: Path, output_dir: Path) -> bool:
    return bool(
        _load_json_if_exists(run_dir / "run_metadata.json")
        or _load_json_if_exists(output_dir / "run_metadata.json")
    )


def write_run_metadata(args: argparse.Namespace, run_dir: Path, output_dir: Path, nets_dir: Path, figures_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    csv_layout = ensure_csv_output_layout(output_dir)
    experiment_metadata = _build_experiment_metadata(args)
    metadata = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "output_dir": str(output_dir),
        "csv_output_root": str(csv_layout.root),
        "csv_report_dir": str(csv_layout.report),
        "csv_results_dir": str(csv_layout.results),
        "csv_internal_dir": str(csv_layout.internal),
        "nets_dir": str(nets_dir),
        "figures_dir": str(figures_dir),
        "args": vars(args),
        **experiment_metadata,
    }
    (run_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_benchmark_timing(run_dir: Path, timing: dict[str, object]) -> None:
    (run_dir / "benchmark_timing.json").write_text(
        json.dumps(timing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _backup_stale_json(path: Path) -> None:
    if not path.exists():
        return
    stale_dir = path.parent / "stale"
    stale_dir.mkdir(parents=True, exist_ok=True)
    backup = stale_dir / f"{path.name}.{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
    path.replace(backup)


def _is_regeneration_run_metadata(payload: dict[str, object]) -> bool:
    args = dict(payload.get("args", {}) or {})
    return bool(
        args.get("skip_run", False)
        or args.get("skip_demand", False)
        or args.get("skip_networks", False)
        or payload.get("result_reason") == "regeneration_without_original_metadata"
    )


def _is_regeneration_benchmark(payload: dict[str, object]) -> bool:
    completed = list(payload.get("completed_scenarios", []) or [])
    return bool(
        completed == ["post_validation"]
        or payload.get("result_reason") == "regeneration_without_original_metadata"
    )


def _restore_regeneration_metadata_if_needed(run_dir: Path, output_dir: Path) -> None:
    root_run = run_dir / "run_metadata.json"
    root_bench = run_dir / "benchmark_timing.json"
    output_run = output_dir / "run_metadata.json"
    output_bench = output_dir / "benchmark_timing.json"

    root_run_payload = _load_json_if_exists(root_run)
    output_run_payload = _load_json_if_exists(output_run)
    if root_run_payload and output_run_payload:
        root_mode = str(root_run_payload.get("experiment_mode", "") or "")
        output_mode = str(output_run_payload.get("experiment_mode", "") or "")
        root_exact = root_run_payload.get("metrics_exact")
        output_exact = output_run_payload.get("metrics_exact")
        if (
            (root_mode and output_mode and root_mode != output_mode)
            or (isinstance(root_exact, bool) and isinstance(output_exact, bool) and root_exact != output_exact)
            or _is_regeneration_run_metadata(root_run_payload)
        ):
            repaired = dict(output_run_payload)
            repaired["restored_experiment_metadata_from"] = str(output_run)
            for key in (
                "run_dir",
                "output_dir",
                "csv_output_root",
                "csv_report_dir",
                "csv_results_dir",
                "csv_internal_dir",
                "nets_dir",
                "figures_dir",
            ):
                if root_run_payload.get(key) is not None:
                    repaired[key] = root_run_payload.get(key)
            args = dict(repaired.get("args", {}) or {})
            args["skip_run"] = False
            args["skip_demand"] = False
            args["skip_networks"] = False
            repaired["args"] = args
            _backup_stale_json(root_run)
            root_run.write_text(json.dumps(repaired, ensure_ascii=False, indent=2), encoding="utf-8")

    root_bench_payload = _load_json_if_exists(root_bench)
    output_bench_payload = _load_json_if_exists(output_bench)
    if root_bench_payload and output_bench_payload:
        root_mode = str(root_bench_payload.get("experiment_mode", "") or "")
        output_mode = str(output_bench_payload.get("experiment_mode", "") or "")
        root_exact = root_bench_payload.get("metrics_exact")
        output_exact = output_bench_payload.get("metrics_exact")
        root_completed = list(root_bench_payload.get("completed_scenarios", []) or [])
        if (
            (root_mode and output_mode and root_mode != output_mode)
            or (isinstance(root_exact, bool) and isinstance(output_exact, bool) and root_exact != output_exact)
            or root_completed == ["post_validation"]
            or _is_regeneration_benchmark(root_bench_payload)
        ):
            repaired = dict(output_bench_payload)
            repaired["restored_experiment_metadata_from"] = str(output_bench)
            _backup_stale_json(root_bench)
            root_bench.write_text(json.dumps(repaired, ensure_ascii=False, indent=2), encoding="utf-8")


def _prepare_regeneration_environment(args: argparse.Namespace, run_dir: Path, output_dir: Path, nets_dir: Path, figures_dir: Path) -> None:
    ensure_csv_output_layout(output_dir)
    mirror_existing_csv_files(
        output_dir,
        results_files=[
            "simulation_results.csv",
            "simulation_results_seed.csv",
            "simulation_summary.csv",
            "baseline_vs_smart_summary.csv",
            "baseline_smart_seed_results.csv",
            "baseline_smart_summary.csv",
            "local_tradeoff_summary.csv",
            "tradeoff_summary.csv",
            "candidate_quality_report.csv",
            "demand_params.csv",
            "demand_source_audit.csv",
            "route_generation_audit.csv",
            "pedestrian_connectivity_audit.csv",
            "pedestrian_route_connectivity_audit.csv",
            "invalid_pedestrian_candidates.csv",
            "invalid_pedestrian_routes.csv",
            "skipped_pedestrian_routes.csv",
            "road_group_allocation_summary.csv",
            "vehicle_edge_coverage_summary.csv",
            "vehicle_route_generation_audit.csv",
            "demand_validation_summary.csv",
            "pedestrian_flow_policy_validation.csv",
            "vehicle_flow_policy_validation.csv",
            "vehicle_global_coverage_validation.csv",
            "network_boundary_warnings.csv",
        ],
        internal_files=[
            "runtime_progress.csv",
            "extension_events_seed.csv",
            "debug_extension_events.csv",
            "incident_events_seed.csv",
            "debug_incident_events.csv",
            "incident_impact_seed.csv",
            "failed_cases.csv",
        ],
    )
    _restore_regeneration_metadata_if_needed(run_dir, output_dir)


def _maybe_write_run_metadata(args: argparse.Namespace, run_dir: Path, output_dir: Path, nets_dir: Path, figures_dir: Path) -> None:
    regeneration_mode = _is_regeneration_mode(args)
    if not regeneration_mode:
        write_run_metadata(args, run_dir, output_dir, nets_dir, figures_dir)
        return
    if _has_original_execution_metadata(run_dir, output_dir):
        return
    write_run_metadata(args, run_dir, output_dir, nets_dir, figures_dir)
    payload = _load_json_if_exists(run_dir / "run_metadata.json")
    if payload:
        payload["result_status"] = "not_verified"
        payload["result_reason"] = "regeneration_without_original_metadata"
        (run_dir / "run_metadata.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _maybe_write_benchmark_timing(args: argparse.Namespace, run_dir: Path, output_dir: Path, timing: dict[str, object]) -> None:
    regeneration_mode = _is_regeneration_mode(args)
    if regeneration_mode and _load_json_if_exists(run_dir / "benchmark_timing.json"):
        return
    if regeneration_mode and _load_json_if_exists(output_dir / "benchmark_timing.json"):
        return
    if regeneration_mode:
        timing = dict(timing)
        timing["result_status"] = "not_verified"
        timing["result_reason"] = "regeneration_without_original_metadata"
    write_benchmark_timing(run_dir, timing)


def _build_experiment_metadata(args: argparse.Namespace) -> dict[str, object]:
    metric_sample_interval_s = float(getattr(args, "metric_sample_interval", 0.0) or 0.0)
    vehicle_sample_interval_s = float(getattr(args, "vehicle_sample_interval", 0.0) or 0.0)
    metrics_exact = metric_sample_interval_s == 0.0 and vehicle_sample_interval_s == 0.0
    return {
        "experiment_mode": "exact" if metrics_exact else "sampled",
        "metric_sample_interval_s": metric_sample_interval_s,
        "vehicle_sample_interval_s": vehicle_sample_interval_s,
        "metrics_exact": metrics_exact,
        "metrics_interpretation": (
            "exact values; use for exact PET/instantaneous queue claims"
            if metrics_exact
            else "sampled estimate; use for repeated paired comparisons, not exact PET/instantaneous queue claims"
        ),
    }


def _count_csv_data_rows(csv_path: Path) -> int:
    if not csv_path.exists():
        return 0
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header is None:
                return 0
            return sum(1 for row in reader if any(str(cell).strip() for cell in row))
    except Exception:
        return 0


def _count_csv_rows_by_value(csv_path: Path, column_name: str, expected_value: str) -> int:
    if not csv_path.exists():
        return 0
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return 0
            count = 0
            for row in reader:
                if not any(str(value).strip() for value in row.values() if value is not None):
                    continue
                if str(row.get(column_name, "")).strip() == expected_value:
                    count += 1
            return count
    except Exception:
        return 0


def _resolve_results_seed_csv(output_dir: Path) -> Path:
    csv_root = output_dir / "csv" / "results"
    candidates = [
        csv_root / "simulation_results_seed.csv",
        output_dir / "simulation_results_seed.csv",
        output_dir / "per_crosswalk_simulation_results_seed.csv",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _evaluate_run_outcome(output_dir: Path, interrupted: bool) -> dict[str, object]:
    failed_cases_count = _count_csv_data_rows(output_dir / "failed_cases.csv")
    results_seed_csv = _resolve_results_seed_csv(output_dir)
    result_rows = _count_csv_data_rows(results_seed_csv)
    baseline_result_rows = _count_csv_rows_by_value(results_seed_csv, "scenario", "baseline")
    smart_result_rows = _count_csv_rows_by_value(results_seed_csv, "scenario", "smart")
    run_success = (
        failed_cases_count == 0
        and baseline_result_rows >= 1
        and smart_result_rows >= 1
        and not interrupted
    )
    failure_parts: list[str] = []
    if interrupted:
        failure_parts.append("interrupted=true")
    if failed_cases_count > 0:
        failure_parts.append(f"failed_cases_count={failed_cases_count}")
    if result_rows == 0:
        failure_parts.append("result_rows=0")
    if baseline_result_rows < 1:
        failure_parts.append(f"baseline_result_rows={baseline_result_rows}")
    if smart_result_rows < 1:
        failure_parts.append(f"smart_result_rows={smart_result_rows}")
    return {
        "run_success": run_success,
        "failure_reason": "" if run_success else ";".join(failure_parts),
        "failed_cases_count": failed_cases_count,
        "result_rows": result_rows,
        "baseline_result_rows": baseline_result_rows,
        "smart_result_rows": smart_result_rows,
        "partial": not run_success,
    }


def _maybe_validate_demand(args: argparse.Namespace, run_dir: Path, output_dir: Path) -> None:
    if not getattr(args, "validate_demand", False):
        return
    validate_demand_run(run_dir=run_dir, output_dir=output_dir)


def run_pipeline(args: argparse.Namespace) -> None:
    run_dir, output_dir, nets_dir, figures_dir = resolve_run_paths(args)
    output_dir.mkdir(parents=True, exist_ok=True)
    nets_dir.mkdir(parents=True, exist_ok=True)
    figures_dir.mkdir(parents=True, exist_ok=True)
    if _is_regeneration_mode(args):
        _prepare_regeneration_environment(args, run_dir, output_dir, nets_dir, figures_dir)
    _maybe_write_run_metadata(args, run_dir, output_dir, nets_dir, figures_dir)
    print(f"Result run directory: {run_dir}")
    pipeline_t0 = time.perf_counter()
    experiment_metadata = _build_experiment_metadata(args)
    benchmark_timing: dict[str, object] = {
        "network_build_sec": None,
        "demand_generation_sec": None,
        "simulation_baseline_sec": None,
        "simulation_smart_sec": None,
        "post_validation_sec": None,
        "total_sec": None,
        "partial": True,
        "interrupted": False,
        "completed_scenarios": [],
        "current_scenario": "",
        **experiment_metadata,
        "run_success": False,
        "failure_reason": "",
        "failed_cases_count": 0,
        "result_rows": 0,
        "baseline_result_rows": 0,
        "smart_result_rows": 0,
    }
    assumptions_path = getattr(args, "model_assumptions", None) or getattr(args, "model_parameters", None)
    simulation_mode = getattr(args, "simulation_mode", "per_candidate")

    if (
        getattr(args, "generate_implementation_diagnostics", False)
        and not getattr(args, "list_valid_smart_crosswalks", False)
    ):
        check_valid_smart_crosswalks(
            t2_path=args.t2,
            output_dir=output_dir,
            admin_polygon_path=getattr(args, "admin_polygon_path", DEFAULT_JUNGGU_ADMIN_POLYGON_PATH),
            nets_dir=nets_dir,
            max_match_distance_m=getattr(args, "max_match_distance_m", 50.0),
            require_tls=getattr(args, "require_tls", True),
            num_valid_crosswalks=getattr(args, "num_valid_crosswalks", 30),
            buffer_m=getattr(args, "buffer_m", 1000.0),
            corridor_whitelist=getattr(args, "corridor_road_whitelist", None),
            network_mode=getattr(args, "network_mode", "expanded"),
        )
        generate_implementation_diagnostics(output_dir, nets_dir)
        if getattr(args, "generate_c_recovery_reports", False):
            generate_c_recovery_reports(
                output_dir=output_dir,
                nets_dir=nets_dir,
                seed=getattr(args, "c_recovery_seed", 42),
            )
        if getattr(args, "c_recovery_only", False):
            return
        if getattr(args, "implementation_diagnostics_only", False):
            return

    if getattr(args, "list_valid_smart_crosswalks", False):
        print("유효한 스마트 횡단보도 후보 목록을 생성합니다...")
        check_valid_smart_crosswalks(
            t2_path=args.t2,
            output_dir=output_dir,
            admin_polygon_path=getattr(args, "admin_polygon_path", DEFAULT_JUNGGU_ADMIN_POLYGON_PATH),
            nets_dir=nets_dir,
            max_match_distance_m=getattr(args, "max_match_distance_m", 50.0),
            require_tls=getattr(args, "require_tls", True),
            num_valid_crosswalks=getattr(args, "num_valid_crosswalks", 30),
            buffer_m=getattr(args, "buffer_m", 1000.0),
            corridor_whitelist=getattr(args, "corridor_road_whitelist", None),
            network_mode=getattr(args, "network_mode", "expanded"),
        )
        if getattr(args, "generate_implementation_diagnostics", False):
            generate_implementation_diagnostics(output_dir, nets_dir)
        if getattr(args, "generate_c_recovery_reports", False):
            generate_c_recovery_reports(
                output_dir=output_dir,
                nets_dir=nets_dir,
                seed=getattr(args, "c_recovery_seed", 42),
            )
        if getattr(args, "c_recovery_only", False):
            return
        return

    if getattr(args, "generate_c_recovery_reports", False) and getattr(args, "c_recovery_only", False):
        generate_c_recovery_reports(
            output_dir=output_dir,
            nets_dir=nets_dir,
            seed=getattr(args, "c_recovery_seed", 42),
        )
        return

    if simulation_mode == "integrated_selected":
        smart_crosswalk_ids = list(getattr(args, "smart_crosswalk_ids", None) or [])
        if not smart_crosswalk_ids:
            raise ValueError("--simulation_mode integrated_selected 에서는 --smart_crosswalk_ids가 필요합니다.")
        selected = select_smart_crosswalks(
            args.t2,
            smart_crosswalk_ids,
            output_dir,
            getattr(args, "admin_polygon_path", None),
        )
        if args.preprocess_only:
            return
        manifest_path = Path(nets_dir) / "integrated_selected" / "smart_crosswalk_manifest.json"
        if not args.skip_networks:
            benchmark_timing["current_scenario"] = "network_build"
            network_t0 = time.perf_counter()
            manifest_path, _, manifest_df = build_integrated_network_manifest(
                selected,
                nets_dir,
                output_dir,
                force=args.force_networks,
                admin_polygon_path=getattr(args, "admin_polygon_path", None),
                buffer_m=getattr(args, "buffer_m", 1000.0),
                corridor_whitelist=getattr(args, "corridor_road_whitelist", None),
                network_mode=getattr(args, "network_mode", "expanded"),
                from_registry=getattr(args, "registry_mode", "off") != "off",
                registry_path=getattr(args, "registry_path", None),
                registry_mode=getattr(args, "registry_mode", "required"),
                registry_network_version=getattr(args, "registry_network_version", None),
                t2_path=getattr(args, "t2", None),
            )
            benchmark_timing["network_build_sec"] = float(time.perf_counter() - network_t0)
            valid_ids = {str(crosswalk_id) for crosswalk_id in manifest_df["crosswalk_id"].astype(str)}
            cast_list = benchmark_timing.get("completed_scenarios")
            if isinstance(cast_list, list):
                cast_list.append("network_build")
            selected = selected[selected["crosswalk_id"].astype(str).isin(valid_ids)].reset_index(drop=True)
            if selected.empty:
                raise ValueError("실행 가능한 integrated_selected 후보가 없습니다. excluded report를 확인하세요.")
        if not manifest_path.exists():
            raise FileNotFoundError(f"통합망 manifest가 없습니다: {manifest_path}")
        if not args.skip_demand:
            benchmark_timing["current_scenario"] = "demand_generation"
            demand_t0 = time.perf_counter()
            generate_integrated_demand(
                selected,
                manifest_path,
                output_dir,
                tuple(args.seeds),
                args.sim_duration,
                args.warmup,
                step_length=getattr(args, "sumo_step_length", 1.0),
                scenario_name=getattr(args, "scenario_name", DEFAULT_DEMAND_SCENARIO_NAME),
                traffic_counts_csv=getattr(args, "traffic_counts", None),
                representative_day_id=getattr(args, "representative_day_id", None),
                model_parameters_path=assumptions_path,
                vehicle_only=getattr(args, "vehicle_only", False),
            )
            benchmark_timing["demand_generation_sec"] = float(time.perf_counter() - demand_t0)
            cast_list = benchmark_timing.get("completed_scenarios")
            if isinstance(cast_list, list):
                cast_list.append("demand_generation")
        if args.disruption_scenario is not None:
            args.incident_scenario = args.disruption_scenario
        if args.extension_increment is not None:
            args.smart_extension_sec = args.extension_increment
        if not args.skip_run:
            benchmark_timing["current_scenario"] = "simulation"
            simulation_t0 = time.perf_counter()
            try:
                collect_integrated_metrics(
                    selected,
                    manifest_path,
                    output_dir,
                    args.sim_duration,
                    args.warmup,
                    tuple(args.seeds),
                    getattr(args, "traci_step_length", 0.1),
                    getattr(args, "traffic_measure_radius_m", 500.0),
                    getattr(args, "smart_extension_sec", None),
                    getattr(args, "max_extensions", None),
                    getattr(args, "vehicle_arrival_rate_per_hour", None),
                    getattr(args, "saturation_flow_rate_per_hour", 1900.0),
                    getattr(args, "vehicle_arrival_model", "poisson"),
                    getattr(args, "incident_scenario", "best_case"),
                    getattr(args, "enable_random_disruptions", False),
                    getattr(args, "bus_stop_rate_per_hour", 0.0),
                    getattr(args, "illegal_parking_rate_per_hour", 0.0),
                    getattr(args, "minor_incident_rate_per_hour", 0.0),
                    getattr(args, "accident_rate_per_hour", 0.0),
                    assumptions_path,
                    getattr(args, "export_fcd", False),
                    getattr(args, "vehicle_only", False),
                    enable_risk_event_collection=getattr(args, "enable_risk_event_collection", False),
                    risk_event_sample_interval_s=getattr(args, "risk_event_sample_interval_s", 1.0),
                    metric_sample_interval_s=getattr(args, "metric_sample_interval", 0.0),
                    vehicle_sample_interval_s=getattr(args, "vehicle_sample_interval", 0.0),
                    progress_interval_s=getattr(args, "progress_interval", 0.0),
                )
            except KeyboardInterrupt:
                benchmark_timing["interrupted"] = True
                benchmark_timing["partial"] = True
                _maybe_write_benchmark_timing(args, run_dir, output_dir, benchmark_timing)
                raise
            except Exception:
                benchmark_timing["partial"] = True
                _maybe_write_benchmark_timing(args, run_dir, output_dir, benchmark_timing)
                raise
            benchmark_timing["simulation_baseline_sec"] = None
            benchmark_timing["simulation_smart_sec"] = None
            benchmark_timing["integrated_simulation_sec"] = float(time.perf_counter() - simulation_t0)
            cast_list = benchmark_timing.get("completed_scenarios")
            if isinstance(cast_list, list):
                cast_list.append("simulation")
        try:
            dump_parameter_table(
                load_model_parameters_file(assumptions_path) if assumptions_path else load_model_parameters_file(None),
                output_dir / "model_assumptions_used.csv",
            )
            (output_dir / "calibration_report.csv").write_text("", encoding="utf-8")
            (output_dir / "calibration_summary.md").write_text("", encoding="utf-8")
            if not args.skip_reports:
                generate_integrated_reports(
                    selected,
                    output_dir,
                    figures_dir,
                    assumptions_path,
                )
            validation_t0 = time.perf_counter()
            _maybe_validate_demand(args, run_dir, output_dir)
            benchmark_timing["post_validation_sec"] = float(time.perf_counter() - validation_t0)
            cast_list = benchmark_timing.get("completed_scenarios")
            if isinstance(cast_list, list):
                cast_list.append("post_validation")
            benchmark_timing["total_sec"] = float(time.perf_counter() - pipeline_t0)
            benchmark_timing["current_scenario"] = ""
            benchmark_timing.update(_evaluate_run_outcome(output_dir, bool(benchmark_timing.get("interrupted", False))))
            _maybe_write_benchmark_timing(args, run_dir, output_dir, benchmark_timing)
        except KeyboardInterrupt:
            benchmark_timing["interrupted"] = True
            benchmark_timing.update(_evaluate_run_outcome(output_dir, True))
            benchmark_timing["partial"] = True
            _maybe_write_benchmark_timing(args, run_dir, output_dir, benchmark_timing)
            raise
        except Exception as exc:
            benchmark_timing.update(_evaluate_run_outcome(output_dir, bool(benchmark_timing.get("interrupted", False))))
            exception_reason = f"exception={exc.__class__.__name__}:{exc}"
            existing_reason = str(benchmark_timing.get("failure_reason") or "")
            benchmark_timing["run_success"] = False
            benchmark_timing["failure_reason"] = (
                f"{exception_reason};{existing_reason}" if existing_reason else exception_reason
            )
            benchmark_timing["partial"] = True
            _maybe_write_benchmark_timing(args, run_dir, output_dir, benchmark_timing)
            raise
        return

    candidates, _, _ = preprocess_inputs(
        args.t1,
        args.t2,
        output_dir,
        args.top_n,
        getattr(args, "target_crosswalk_ids", None),
    )
    candidates_csv = output_dir / "candidates.csv"

    if args.preprocess_only:
        return

    if not args.skip_networks:
        benchmark_timing["current_scenario"] = "network_build"
        network_t0 = time.perf_counter()
        build_all_networks(
            candidates_csv,
            nets_dir,
            output_dir,
            force=args.force_networks,
            reuse_nets_dir=getattr(args, "reuse_nets_dir", None),
            network_radius_m=getattr(args, "network_radius_m", None),
            network_mode=getattr(args, "network_mode", "local_radius"),
            admin_polygon_path=getattr(args, "admin_polygon_path", None),
            buffer_m=getattr(args, "buffer_m", 1000.0),
            corridor_whitelist=getattr(args, "corridor_road_whitelist", None),
        )
        benchmark_timing["network_build_sec"] = float(time.perf_counter() - network_t0)
        cast_list = benchmark_timing.get("completed_scenarios")
        if isinstance(cast_list, list):
            cast_list.append("network_build")

    if not args.skip_demand:
        benchmark_timing["current_scenario"] = "demand_generation"
        demand_t0 = time.perf_counter()
        generate_for_candidates(
            candidates_csv,
            nets_dir,
            output_dir,
            tuple(args.seeds),
            args.sim_duration,
            args.warmup,
            step_length=getattr(args, "sumo_step_length", 1.0),
            scenario_name=getattr(args, "scenario_name", DEFAULT_DEMAND_SCENARIO_NAME),
            traffic_counts_csv=getattr(args, "traffic_counts", None),
            representative_day_id=getattr(args, "representative_day_id", None),
            model_parameters_path=assumptions_path,
            vehicle_only=getattr(args, "vehicle_only", False),
            reuse_demand_dir=getattr(args, "reuse_demand_dir", None),
            force_demand=getattr(args, "force_demand", False),
        )
        benchmark_timing["demand_generation_sec"] = float(time.perf_counter() - demand_t0)
        cast_list = benchmark_timing.get("completed_scenarios")
        if isinstance(cast_list, list):
            cast_list.append("demand_generation")

    if args.disruption_scenario is not None:
        args.incident_scenario = args.disruption_scenario
    if args.extension_increment is not None:
        args.smart_extension_sec = args.extension_increment

    if not args.skip_run:
        benchmark_timing["current_scenario"] = "simulation"
        simulation_t0 = time.perf_counter()
        try:
            _, _, simulation_timing = collect_all(
                candidates_csv,
                output_dir,
                nets_dir,
                args.sim_duration,
                args.warmup,
                tuple(args.seeds),
                getattr(args, "traci_step_length", 0.1),
                getattr(args, "traffic_measure_radius_m", 500.0),
                getattr(args, "smart_extension_sec", None),
                getattr(args, "max_extensions", None),
                getattr(args, "vehicle_arrival_rate_per_hour", None),
                getattr(args, "saturation_flow_rate_per_hour", 1900.0),
                getattr(args, "vehicle_arrival_model", "poisson"),
                getattr(args, "incident_scenario", "best_case"),
                getattr(args, "enable_random_disruptions", False),
                getattr(args, "bus_stop_rate_per_hour", 0.0),
                getattr(args, "illegal_parking_rate_per_hour", 0.0),
                getattr(args, "minor_incident_rate_per_hour", 0.0),
                getattr(args, "accident_rate_per_hour", 0.0),
                assumptions_path,
                getattr(args, "export_fcd", False),
                getattr(args, "vehicle_only", False),
                metric_sample_interval_s=getattr(args, "metric_sample_interval", 0.0),
                vehicle_sample_interval_s=getattr(args, "vehicle_sample_interval", 0.0),
                progress_interval_s=getattr(args, "progress_interval", 0.0),
            )
        except KeyboardInterrupt:
            benchmark_timing["interrupted"] = True
            benchmark_timing["partial"] = True
            _maybe_write_benchmark_timing(args, run_dir, output_dir, benchmark_timing)
            raise
        except Exception:
            benchmark_timing["partial"] = True
            _maybe_write_benchmark_timing(args, run_dir, output_dir, benchmark_timing)
            raise
        benchmark_timing["simulation_baseline_sec"] = simulation_timing.get("simulation_baseline_sec")
        benchmark_timing["simulation_smart_sec"] = simulation_timing.get("simulation_smart_sec")
        benchmark_timing["simulation_total_sec"] = float(time.perf_counter() - simulation_t0)
        cast_list = benchmark_timing.get("completed_scenarios")
        if isinstance(cast_list, list):
            cast_list.append("simulation")

    try:
        dump_parameter_table(
            load_model_parameters_file(assumptions_path) if assumptions_path else load_model_parameters_file(None),
            output_dir / "model_assumptions_used.csv",
        )

        calib_counts = getattr(args, "traffic_counts", None)
        calib_day = getattr(args, "representative_day_id", None)
        if getattr(args, "calibration_config", None):
            try:
                calib_cfg = load_model_parameters_file(args.calibration_config)
                calib_counts = calib_cfg.get("observed_counts", {}).get("value", calib_counts)
                calib_day = calib_cfg.get("representative_day_id", {}).get("value", calib_day)
            except Exception:
                pass

        calibrate(
            output_dir,
            calib_counts,
            calib_day,
        )

        if getattr(args, "export_gui", False):
            export_visual_assets(
                output_dir,
                nets_dir,
                figures_dir,
                candidates_csv,
                tuple(args.seeds),
            )

        benchmark_timing["current_scenario"] = "post_validation"
        validation_t0 = time.perf_counter()
        _maybe_validate_demand(args, run_dir, output_dir)
        benchmark_timing["post_validation_sec"] = float(time.perf_counter() - validation_t0)
        cast_list = benchmark_timing.get("completed_scenarios")
        if isinstance(cast_list, list):
            cast_list.append("post_validation")

        if not args.skip_reports:
            generate_all_reports(
                output_dir,
                figures_dir,
                candidates_csv,
                nets_dir,
                assumptions_path,
                getattr(args, "simulation_mode", None),
            )

        benchmark_timing["total_sec"] = float(time.perf_counter() - pipeline_t0)
        benchmark_timing["current_scenario"] = ""
        benchmark_timing.update(_evaluate_run_outcome(output_dir, bool(benchmark_timing.get("interrupted", False))))
        _maybe_write_benchmark_timing(args, run_dir, output_dir, benchmark_timing)
    except KeyboardInterrupt:
        benchmark_timing["interrupted"] = True
        benchmark_timing.update(_evaluate_run_outcome(output_dir, True))
        benchmark_timing["partial"] = True
        _maybe_write_benchmark_timing(args, run_dir, output_dir, benchmark_timing)
        raise
    except Exception as exc:
        benchmark_timing.update(_evaluate_run_outcome(output_dir, bool(benchmark_timing.get("interrupted", False))))
        exception_reason = f"exception={exc.__class__.__name__}:{exc}"
        existing_reason = str(benchmark_timing.get("failure_reason") or "")
        benchmark_timing["run_success"] = False
        benchmark_timing["failure_reason"] = (
            f"{exception_reason};{existing_reason}" if existing_reason else exception_reason
        )
        benchmark_timing["partial"] = True
        _maybe_write_benchmark_timing(args, run_dir, output_dir, benchmark_timing)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="서울 중구 스마트 횡단보도 SUMO 시뮬레이션")
    parser.add_argument("--t1", default=str(BASE_DIR / "data" / "T1_accident_crosswalk.csv"))
    parser.add_argument("--t2", default=str(BASE_DIR / "data" / "T2_crosswalk_features.csv"))
    parser.add_argument(
        "--simulation_mode",
        choices=["per_candidate", "integrated_selected"],
        default="per_candidate",
    )
    parser.add_argument("--top_n", type=int, default=20)
    parser.add_argument("--target_crosswalk_ids", nargs="+", default=None)
    parser.add_argument("--smart_crosswalk_ids", nargs="+", default=None)
    parser.add_argument("--seeds", nargs="+", type=int, default=[42, 43, 44])
    parser.add_argument("--sim_duration", type=int, default=1800)
    parser.add_argument("--warmup", type=int, default=300)
    parser.add_argument("--sumo_step_length", type=float, default=1.0)
    parser.add_argument("--traci_step_length", type=float, default=0.1)
    parser.add_argument("--traffic_measure_radius_m", type=float, default=500.0)

    parser.add_argument("--network_radius_m", type=float, default=None)
    parser.add_argument(
        "--network_mode",
        choices=["local", "expanded", "local_radius", "jungu_admin_buffer", "corridor_network"],
        default="expanded",
    )
    parser.add_argument("--admin_polygon_path", default=str(DEFAULT_JUNGGU_ADMIN_POLYGON_PATH))
    parser.add_argument("--buffer_m", type=float, default=1000.0)
    parser.add_argument("--corridor_road_whitelist", nargs="*", default=None)
    parser.add_argument("--reuse_nets_dir", default=None)
    parser.add_argument(
        "--registry_path",
        default=str(BASE_DIR / "registry" / "junggu_crosswalk_sumo_registry.csv"),
    )
    parser.add_argument(
        "--registry_mode",
        choices=["required", "prefer", "off"],
        default="required",
    )
    parser.add_argument("--registry_network_version", default=None)

    parser.add_argument("--scenario_name", default=DEFAULT_DEMAND_SCENARIO_NAME)
    parser.add_argument("--demand_profile", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--traffic_counts", default=None)
    parser.add_argument("--representative_day_id", default=None)

    parser.add_argument("--smart_extension_sec", type=float, default=5.0)
    parser.add_argument(
        "--extension_increment",
        type=float,
        default=None,
        help="Backward compatible alias for --smart_extension_sec",
    )
    parser.add_argument("--max_extensions", type=int, default=1)
    parser.add_argument("--vehicle_arrival_rate_per_hour", type=float, default=None)
    parser.add_argument("--saturation_flow_rate_per_hour", type=float, default=1900.0)
    parser.add_argument("--vehicle_arrival_model", choices=["poisson", "bernoulli"], default="poisson")

    parser.add_argument(
        "--incident_scenario",
        choices=["best_case", "normal_urban", "congested_urban", "incident_case"],
        default="normal_urban",
    )
    parser.add_argument(
        "--disruption_scenario",
        choices=["best_case", "normal_urban", "congested_urban", "incident_case"],
        default=None,
        help="Backward compatible alias for --incident_scenario",
    )
    parser.add_argument("--enable_random_disruptions", action="store_true")
    parser.add_argument("--bus_stop_rate_per_hour", type=float, default=0.0)
    parser.add_argument("--illegal_parking_rate_per_hour", type=float, default=0.0)
    parser.add_argument("--minor_incident_rate_per_hour", type=float, default=0.0)
    parser.add_argument("--accident_rate_per_hour", type=float, default=0.0)

    parser.add_argument("--model_assumptions", default=str(BASE_DIR / "config" / "model_assumptions.yaml"))
    parser.add_argument(
        "--model_parameters",
        default=None,
        help="Backward compatible alias for --model_assumptions",
    )
    parser.add_argument("--calibration_config", default=None)

    parser.add_argument("--export_gui", action="store_true")
    parser.add_argument("--export_fcd", action="store_true")
    parser.add_argument("--vehicle_only", action="store_true")

    parser.add_argument(
        "--enable_risk_event_collection",
        action="store_true",
        help="integrated_selected 모드에서 차량-보행자 위험 이벤트 수집 활성화 (기본: 비활성)",
    )
    parser.add_argument(
        "--risk_event_sample_interval_s",
        type=float,
        default=1.0,
        help="위험 이벤트 프레임 수집 간격 (초). --enable_risk_event_collection 활성 시만 사용 (기본: 1.0)",
    )

    parser.add_argument("--result_root", default=str(DEFAULT_RESULT_ROOT))
    parser.add_argument("--run_name", default=None)
    parser.add_argument("--run_dir", default=None)
    parser.add_argument("--output_dir", default=None)
    parser.add_argument("--nets_dir", default=None)
    parser.add_argument("--figures_dir", default=None)
    parser.add_argument("--force_networks", action="store_true")
    parser.add_argument("--reuse_demand_dir", default=None)
    parser.add_argument("--force_demand", action="store_true")
    parser.add_argument(
        "--metric-sample-interval",
        type=float,
        default=0.0,
        help="Lane-level heavy metrics sampling interval in seconds. 0 means every step.",
    )
    parser.add_argument(
        "--vehicle-sample-interval",
        type=float,
        default=0.0,
        help="Vehicle-heavy metric sampling interval in seconds. 0 means every step.",
    )
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=0.0,
        help="Progress heartbeat interval in seconds. 0 disables progress logging.",
    )
    parser.add_argument("--preprocess_only", action="store_true")
    parser.add_argument("--skip_networks", action="store_true")
    parser.add_argument("--skip_demand", action="store_true")
    parser.add_argument("--skip_run", action="store_true")
    parser.add_argument("--skip_reports", action="store_true")
    parser.add_argument("--validate_demand", action="store_true")

    parser.add_argument("--list_valid_smart_crosswalks", action="store_true")
    parser.add_argument("--generate_implementation_diagnostics", action="store_true")
    parser.add_argument("--implementation_diagnostics_only", action="store_true")
    parser.add_argument("--generate_c_recovery_reports", action="store_true")
    parser.add_argument("--c_recovery_only", action="store_true")
    parser.add_argument("--c_recovery_seed", type=int, default=42)
    parser.add_argument("--num_valid_crosswalks", type=int, default=30)
    parser.add_argument("--max_match_distance_m", type=float, default=50.0)
    parser.add_argument("--require_tls", type=lambda x: str(x).lower() == 'true', default=True)

    return parser.parse_args()



def main() -> None:
    run_pipeline(parse_args())


if __name__ == "__main__":
    main()
