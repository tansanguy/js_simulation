from __future__ import annotations

import argparse
import json
import shutil
import sys
from argparse import Namespace
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SMART_DIR = PROJECT_DIR / "smart_crosswalk_sumo"
DEFAULT_REUSE_NETS = PROJECT_DIR / "result" / "report_top12_seed4_2h" / "sumo_nets"

sys.path.insert(0, str(PROJECT_DIR))

from smart_crosswalk_sumo.main import run_pipeline  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="보고서용 스마트 횡단보도 SUMO 시뮬레이션 실행 프리셋"
    )
    parser.add_argument("--run_name", default="report_ready_top12_seed4_30min")
    parser.add_argument("--top_n", type=int, default=12)
    parser.add_argument("--seeds", nargs="+", type=int, default=[42, 43, 44, 45])
    parser.add_argument("--sim_duration", type=int, default=1800)
    parser.add_argument("--warmup", type=int, default=300)
    parser.add_argument(
        "--traci_step_length",
        type=float,
        default=0.5,
        help="TraCI simulation step length. 보고서용 기본값은 속도와 정밀도의 균형을 위해 0.5초.",
    )
    parser.add_argument(
        "--demand_profile",
        default="average",
        choices=["average", "seoul_commute_peak"],
        help="수요 생성 프로파일. 출퇴근 trade-off 분석에는 seoul_commute_peak를 사용한다.",
    )
    parser.add_argument("--result_root", default=str(PROJECT_DIR / "result"))
    parser.add_argument(
        "--reuse_nets_dir",
        default=str(DEFAULT_REUSE_NETS),
        help="기존 SUMO 네트워크 폴더. 없으면 네트워크를 새로 생성한다.",
    )
    parser.add_argument(
        "--build_networks",
        action="store_true",
        help="기존 네트워크 재사용 대신 OSM 다운로드/netconvert를 다시 수행한다.",
    )
    parser.add_argument("--force_networks", action="store_true")
    return parser.parse_args()


def write_preset_note(run_dir: Path, args: argparse.Namespace, pipeline_args: Namespace) -> None:
    note = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "purpose": "보고서용 baseline/smart 비교 결과 생성",
        "preset": {
            "top_n": args.top_n,
            "seeds": args.seeds,
            "sim_duration": args.sim_duration,
            "warmup": args.warmup,
            "traci_step_length": args.traci_step_length,
            "demand_profile": args.demand_profile,
            "analysis_minutes": args.sim_duration / 60,
            "sumo_runs": args.top_n * len(args.seeds) * 2,
        },
        "network_mode": "build" if args.build_networks else "reuse",
        "reuse_nets_dir": None if args.build_networks else args.reuse_nets_dir,
        "pipeline_args": vars(pipeline_args),
    }
    (run_dir / "report_preset.json").write_text(
        json.dumps(note, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def prepared_nets_dir(args: argparse.Namespace) -> str | None:
    if args.build_networks:
        return None

    reuse_nets_dir = Path(args.reuse_nets_dir)
    if not reuse_nets_dir.exists():
        return None

    run_dir = Path(args.result_root) / args.run_name
    target_nets_dir = run_dir / "sumo_nets"
    if not target_nets_dir.exists():
        run_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(reuse_nets_dir, target_nets_dir)
    return str(target_nets_dir)


def build_pipeline_args(args: argparse.Namespace, nets_dir: str | None) -> Namespace:
    use_reused_networks = nets_dir is not None

    return Namespace(
        t1=str(SMART_DIR / "data" / "T1_accident_crosswalk.csv"),
        t2=str(SMART_DIR / "data" / "T2_crosswalk_features.csv"),
        top_n=args.top_n,
        seeds=args.seeds,
        sim_duration=args.sim_duration,
        warmup=args.warmup,
        traci_step_length=args.traci_step_length,
        demand_profile=args.demand_profile,
        result_root=args.result_root,
        run_name=args.run_name,
        run_dir=None,
        output_dir=None,
        nets_dir=nets_dir,
        figures_dir=None,
        force_networks=args.force_networks,
        preprocess_only=False,
        skip_networks=use_reused_networks,
        skip_demand=False,
        skip_run=False,
        skip_reports=False,
    )


def main() -> None:
    args = parse_args()
    nets_dir = prepared_nets_dir(args)
    pipeline_args = build_pipeline_args(args, nets_dir)
    run_dir = Path(args.result_root) / args.run_name
    print("[report-run] configuration")
    print(f"  run_dir={run_dir}")
    print(f"  top_n={args.top_n}")
    print(f"  seeds={args.seeds}")
    print(f"  sim_duration={args.sim_duration}")
    print(f"  warmup={args.warmup}")
    print(f"  traci_step_length={args.traci_step_length}")
    print(f"  demand_profile={args.demand_profile}")
    print(f"  skip_networks={pipeline_args.skip_networks}")
    if pipeline_args.skip_networks:
        print(f"  reuse_nets_dir={pipeline_args.nets_dir}")

    run_pipeline(pipeline_args)
    write_preset_note(run_dir, args, pipeline_args)

    print("[report-run] done")
    print(f"  outputs={run_dir / 'outputs'}")
    print(f"  figures={run_dir / 'figures'}")


if __name__ == "__main__":
    main()
