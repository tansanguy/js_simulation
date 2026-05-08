from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

try:
    from .model_config import load_model_parameters as load_model_parameters_file
except ImportError:
    from model_config import load_model_parameters as load_model_parameters_file

try:
    from .build_networks import build_all_networks
    from .calibration import calibrate
    from .collect_metrics import collect_all
    from .generate_demand import generate_for_candidates
    from .generate_reports import generate_all_reports
    from .model_config import dump_parameter_table
    from .preprocess import preprocess_inputs
    from .visualization_exports import export_visual_assets
except ImportError:
    from build_networks import build_all_networks
    from calibration import calibrate
    from collect_metrics import collect_all
    from generate_demand import generate_for_candidates
    from generate_reports import generate_all_reports
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


def write_run_metadata(args: argparse.Namespace, run_dir: Path, output_dir: Path, nets_dir: Path, figures_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "output_dir": str(output_dir),
        "nets_dir": str(nets_dir),
        "figures_dir": str(figures_dir),
        "args": vars(args),
    }
    (run_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_pipeline(args: argparse.Namespace) -> None:
    run_dir, output_dir, nets_dir, figures_dir = resolve_run_paths(args)
    output_dir.mkdir(parents=True, exist_ok=True)
    nets_dir.mkdir(parents=True, exist_ok=True)
    figures_dir.mkdir(parents=True, exist_ok=True)
    write_run_metadata(args, run_dir, output_dir, nets_dir, figures_dir)
    print(f"Result run directory: {run_dir}")
    assumptions_path = getattr(args, "model_assumptions", None) or getattr(args, "model_parameters", None)

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
        build_all_networks(
            candidates_csv,
            nets_dir,
            output_dir,
            force=args.force_networks,
            network_radius_m=getattr(args, "network_radius_m", None),
            network_mode=getattr(args, "network_mode", "local_radius"),
            admin_polygon_path=getattr(args, "admin_polygon_path", None),
            buffer_m=getattr(args, "buffer_m", 1000.0),
            corridor_whitelist=getattr(args, "corridor_road_whitelist", None),
        )

    if not args.skip_demand:
        generate_for_candidates(
            candidates_csv,
            nets_dir,
            output_dir,
            tuple(args.seeds),
            args.sim_duration,
            args.warmup,
            step_length=getattr(args, "sumo_step_length", 1.0),
            demand_profile=getattr(args, "demand_profile", "average"),
            traffic_counts_csv=getattr(args, "traffic_counts", None),
            representative_day_id=getattr(args, "representative_day_id", None),
            model_parameters_path=assumptions_path,
            vehicle_only=getattr(args, "vehicle_only", False),
        )

    if args.disruption_scenario is not None:
        args.incident_scenario = args.disruption_scenario
    if args.extension_increment is not None:
        args.smart_extension_sec = args.extension_increment

    if not args.skip_run:
        collect_all(
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
        )

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

    if not args.skip_reports:
        generate_all_reports(
            output_dir,
            figures_dir,
            candidates_csv,
            nets_dir,
            assumptions_path,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="서울 중구 스마트 횡단보도 SUMO 시뮬레이션")
    parser.add_argument("--t1", default=str(BASE_DIR / "data" / "T1_accident_crosswalk.csv"))
    parser.add_argument("--t2", default=str(BASE_DIR / "data" / "T2_crosswalk_features.csv"))
    parser.add_argument("--top_n", type=int, default=20)
    parser.add_argument("--target_crosswalk_ids", nargs="+", default=None)
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

    parser.add_argument("--demand_profile", default="average")
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

    parser.add_argument("--result_root", default=str(DEFAULT_RESULT_ROOT))
    parser.add_argument("--run_name", default=None)
    parser.add_argument("--run_dir", default=None)
    parser.add_argument("--output_dir", default=None)
    parser.add_argument("--nets_dir", default=None)
    parser.add_argument("--figures_dir", default=None)
    parser.add_argument("--force_networks", action="store_true")
    parser.add_argument("--preprocess_only", action="store_true")
    parser.add_argument("--skip_networks", action="store_true")
    parser.add_argument("--skip_demand", action="store_true")
    parser.add_argument("--skip_run", action="store_true")
    parser.add_argument("--skip_reports", action="store_true")
    return parser.parse_args()



def main() -> None:
    run_pipeline(parse_args())


if __name__ == "__main__":
    main()
