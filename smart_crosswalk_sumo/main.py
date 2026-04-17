from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

try:
    from .build_networks import build_all_networks
    from .collect_metrics import collect_all
    from .generate_demand import generate_for_candidates
    from .generate_reports import generate_all_reports
    from .preprocess import preprocess_inputs
except ImportError:
    from build_networks import build_all_networks
    from collect_metrics import collect_all
    from generate_demand import generate_for_candidates
    from generate_reports import generate_all_reports
    from preprocess import preprocess_inputs


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
DEFAULT_RESULT_ROOT = PROJECT_DIR / "result"


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

    candidates, _, _ = preprocess_inputs(args.t1, args.t2, output_dir, args.top_n)
    candidates_csv = output_dir / "candidates.csv"

    if args.preprocess_only:
        return

    if not args.skip_networks:
        build_all_networks(candidates_csv, nets_dir, output_dir, force=args.force_networks)

    if not args.skip_demand:
        generate_for_candidates(
            candidates_csv,
            nets_dir,
            output_dir,
            tuple(args.seeds),
            args.sim_duration,
            args.warmup,
            demand_profile=getattr(args, "demand_profile", "average"),
        )

    if not args.skip_run:
        collect_all(
            candidates_csv,
            output_dir,
            nets_dir,
            args.sim_duration,
            args.warmup,
            tuple(args.seeds),
            getattr(args, "traci_step_length", 0.1),
        )

    if not args.skip_reports:
        generate_all_reports(output_dir, figures_dir, candidates_csv)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="서울 중구 스마트 횡단보도 SUMO 시뮬레이션 새 모델"
    )
    parser.add_argument(
        "--t1",
        default=str(BASE_DIR / "data" / "T1_accident_crosswalk.csv"),
    )
    parser.add_argument(
        "--t2",
        default=str(BASE_DIR / "data" / "T2_crosswalk_features.csv"),
    )
    parser.add_argument("--top_n", type=int, default=20)
    parser.add_argument("--seeds", nargs="+", type=int, default=[42, 43, 44])
    parser.add_argument("--sim_duration", type=int, default=1800)
    parser.add_argument("--warmup", type=int, default=300)
    parser.add_argument("--traci_step_length", type=float, default=0.1)
    parser.add_argument("--demand_profile", default="average")
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
