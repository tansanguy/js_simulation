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
DEFAULT_JUNGGU_ADMIN_POLYGON_PATH = SMART_DIR / "data" / "junggu_admin_boundary.geojson"

sys.path.insert(0, str(PROJECT_DIR))

from smart_crosswalk_sumo.main import run_pipeline  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="보고서용 스마트 횡단보도 SUMO 시뮬레이션 실행 프리셋"
    )
    parser.add_argument("--run_name", default="report_ready_top12_seed4_30min")
    parser.add_argument("--top_n", type=int, default=12)
    parser.add_argument("--target_crosswalk_ids", nargs="+", default=None)
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
        "--traffic_measure_radius_m",
        type=float,
        default=500.0,
        help="차량 파급효과를 집계할 횡단보도 중심 반경(m).",
    )
    parser.add_argument(
        "--extension_increment",
        type=float,
        default=5.0,
        help="스마트 신호 1회 연장 시간(초). 기본값은 5초 1회.",
    )
    parser.add_argument("--max_extensions", type=int, default=1)
    parser.add_argument(
        "--network_radius_m",
        type=float,
        default=None,
        help="새 네트워크 생성 시 OSM bbox 반경(m). 주변 파급효과 분석에는 300~500m 이상 권장.",
    )
    parser.add_argument(
        "--network_mode",
        choices=["local", "expanded", "local_radius", "jungu_admin_buffer", "corridor_network"],
        default="expanded",
        help="기본값은 중구 전체 + 인접 도로를 포함하는 expanded 네트워크.",
    )
    parser.add_argument(
        "--admin_polygon_path",
        default=str(DEFAULT_JUNGGU_ADMIN_POLYGON_PATH),
        help="행정경계 GeoJSON 경로. network_mode=jungu_admin_buffer에서 사용.",
    )
    parser.add_argument(
        "--buffer_m",
        type=float,
        default=1000.0,
        help="행정경계 바깥 연결도로를 포함하기 위한 buffer(m).",
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
            "target_crosswalk_ids": args.target_crosswalk_ids,
            "seeds": args.seeds,
            "sim_duration": args.sim_duration,
            "warmup": args.warmup,
            "traci_step_length": args.traci_step_length,
            "traffic_measure_radius_m": args.traffic_measure_radius_m,
            "extension_increment": args.extension_increment,
            "max_extensions": args.max_extensions,
            "network_radius_m": args.network_radius_m,
            "demand_profile": args.demand_profile,
            "analysis_minutes": args.sim_duration / 60,
            "sumo_runs": (len(args.target_crosswalk_ids) if args.target_crosswalk_ids else args.top_n)
            * len(args.seeds)
            * 2,
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
    if args.build_networks or args.network_mode not in {"local", "local_radius"}:
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
        target_crosswalk_ids=args.target_crosswalk_ids,
        seeds=args.seeds,
        sim_duration=args.sim_duration,
        warmup=args.warmup,
        traci_step_length=args.traci_step_length,
        traffic_measure_radius_m=args.traffic_measure_radius_m,
        extension_increment=args.extension_increment,
        max_extensions=args.max_extensions,
        network_radius_m=args.network_radius_m,
        network_mode=args.network_mode,
        admin_polygon_path=args.admin_polygon_path,
        buffer_m=args.buffer_m,
        demand_profile=args.demand_profile,
        traffic_counts=None,
        representative_day_id=None,
        sumo_step_length=1.0,
        incident_scenario="normal_urban",
        disruption_scenario=None,
        smart_extension_sec=None,
        vehicle_arrival_rate_per_hour=None,
        saturation_flow_rate_per_hour=1900.0,
        vehicle_arrival_model="poisson",
        enable_random_disruptions=False,
        bus_stop_rate_per_hour=0.0,
        illegal_parking_rate_per_hour=0.0,
        minor_incident_rate_per_hour=0.0,
        accident_rate_per_hour=0.0,
        model_assumptions=str(SMART_DIR / "config" / "model_assumptions.yaml"),
        model_parameters=None,
        calibration_config=None,
        result_root=args.result_root,
        run_name=args.run_name,
        run_dir=None,
        output_dir=None,
        nets_dir=nets_dir,
        figures_dir=None,
        export_gui=False,
        export_fcd=False,
        vehicle_only=False,
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
    print(f"  target_crosswalk_ids={args.target_crosswalk_ids}")
    print(f"  seeds={args.seeds}")
    print(f"  sim_duration={args.sim_duration}")
    print(f"  warmup={args.warmup}")
    print(f"  traci_step_length={args.traci_step_length}")
    print(f"  traffic_measure_radius_m={args.traffic_measure_radius_m}")
    print(f"  extension_increment={args.extension_increment}")
    print(f"  max_extensions={args.max_extensions}")
    print(f"  network_radius_m={args.network_radius_m}")
    print(f"  network_mode={args.network_mode}")
    print(f"  admin_polygon_path={args.admin_polygon_path}")
    print(f"  buffer_m={args.buffer_m}")
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
