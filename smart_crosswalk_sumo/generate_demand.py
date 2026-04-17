from __future__ import annotations

import argparse
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from .network_utils import load_metadata, normalized_sumo_home, sumo_env
except ImportError:
    from network_utils import load_metadata, normalized_sumo_home, sumo_env


DEMAND_PROFILES = {
    "average": {
        "veh_multiplier": 1.0,
        "ped_lambda_min": 100,
        "ped_lambda_max": 600,
    },
    "seoul_commute_peak": {
        # Report stress profile: approximate Seoul AM/PM peak conditions by
        # increasing commuter vehicle pressure and pedestrian arrivals together.
        "veh_multiplier": 2.0,
        "ped_lambda_min": 650,
        "ped_lambda_max": 1100,
    },
}


def get_demand_params(row: pd.Series, seed: int, demand_profile: str = "average") -> dict[str, float | str]:
    if demand_profile not in DEMAND_PROFILES:
        valid = ", ".join(sorted(DEMAND_PROFILES))
        raise ValueError(f"Unknown demand_profile={demand_profile!r}. Valid profiles: {valid}")

    profile = DEMAND_PROFILES[demand_profile]
    rng = np.random.default_rng(seed)
    aadt_noisy = max(1000, float(row["추정AADT"]) + int(rng.integers(-3000, 3001)))
    hourly_veh = aadt_noisy / 24 / max(float(row["LANES"]), 1.0)
    veh_per_hour = hourly_veh * 1.8 * float(profile["veh_multiplier"])
    ped_lambda = int(rng.integers(int(profile["ped_lambda_min"]), int(profile["ped_lambda_max"]) + 1))
    return {
        "demand_profile": demand_profile,
        "veh_per_hour": float(veh_per_hour),
        "ped_mean_gap_sec": float(3600 / ped_lambda),
        "elderly_ratio": float(row["노인비율"]),
        "ped_lambda": float(ped_lambda),
    }


def random_trips_script() -> str:
    sumo_home = normalized_sumo_home()
    if not sumo_home:
        raise RuntimeError("SUMO_HOME이 설정되어 있지 않습니다.")
    candidates = [
        Path(sumo_home) / "tools" / "randomTrips.py",
        Path(sumo_home) / "share" / "sumo" / "tools" / "randomTrips.py",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(f"randomTrips.py를 찾지 못했습니다: {sumo_home}")


def generate_vehicle_routes(
    params: dict[str, float],
    net_file: str | Path,
    output_file: str | Path,
    sim_duration: int = 1800,
    seed: int = 42,
) -> None:
    period = max(0.1, 3600 / max(float(params["veh_per_hour"]), 1.0))
    output_file = Path(output_file)
    trip_file = output_file.with_name(output_file.stem.replace(".rou", "") + ".trips.xml")
    log_file_path = output_file.with_name(f"demand_seed{seed}.log")
    with log_file_path.open("w", encoding="utf-8") as log_file:
        subprocess.run(
            [
                "python3",
                random_trips_script(),
                "-n",
                str(net_file),
                "-o",
                str(trip_file),
                "-r",
                str(output_file),
                "--period",
                str(period),
                "--seed",
                str(seed),
                "--begin",
                "0",
                "--end",
                str(sim_duration),
                "--vehicle-class",
                "passenger",
                "--validate",
            ],
            check=True,
            env=sumo_env(),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )


def generate_pedestrian_demand(
    params: dict[str, float],
    ped_route: dict[str, str],
    output_file: str | Path,
    sim_duration: int = 1800,
    seed: int = 42,
) -> int:
    rng = np.random.default_rng(seed + 1000)
    elderly_ratio = float(params["elderly_ratio"])
    mean_gap = float(params["ped_mean_gap_sec"])

    root = ET.Element("routes")
    ET.SubElement(
        root,
        "vType",
        {
            "id": "adult",
            "vClass": "pedestrian",
            "minGap": "0.25",
            "width": "0.5",
            "length": "0.25",
            "maxSpeed": "1.5",
            "speedDev": "0.1",
        },
    )
    ET.SubElement(
        root,
        "vType",
        {
            "id": "elderly",
            "vClass": "pedestrian",
            "minGap": "0.25",
            "width": "0.5",
            "length": "0.25",
            "maxSpeed": "1.13",
            "speedDev": "0.15",
            "color": "255,0,0",
        },
    )

    t = float(rng.exponential(mean_gap))
    ped_id = 0
    while t < sim_duration:
        vtype = "elderly" if rng.random() < elderly_ratio else "adult"
        person = ET.SubElement(
            root,
            "person",
            {"id": f"ped_{ped_id}", "depart": f"{t:.2f}", "type": vtype},
        )
        ET.SubElement(
            person,
            "walk",
            {"from": ped_route["from_edge"], "to": ped_route["to_edge"]},
        )
        t += float(rng.exponential(mean_gap))
        ped_id += 1

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(output_file, encoding="utf-8", xml_declaration=True)
    return ped_id


def write_sumocfg(
    net_file: str | Path,
    vehicle_route_file: str | Path,
    pedestrian_route_file: str | Path,
    output_file: str | Path,
    sim_duration: int,
    warmup: int,
    step_length: float,
) -> None:
    root = ET.Element("configuration")
    input_elem = ET.SubElement(root, "input")
    ET.SubElement(input_elem, "net-file", {"value": str(Path(net_file).resolve())})
    ET.SubElement(
        input_elem,
        "route-files",
        {
            "value": (
                f"{Path(vehicle_route_file).resolve()},"
                f"{Path(pedestrian_route_file).resolve()}"
            )
        },
    )
    time_elem = ET.SubElement(root, "time")
    ET.SubElement(time_elem, "begin", {"value": "0"})
    ET.SubElement(time_elem, "end", {"value": str(sim_duration + warmup)})
    ET.SubElement(time_elem, "step-length", {"value": str(step_length)})
    processing_elem = ET.SubElement(root, "processing")
    ET.SubElement(processing_elem, "time-to-teleport", {"value": "300"})
    ET.SubElement(processing_elem, "pedestrian.model", {"value": "striping"})
    report_elem = ET.SubElement(root, "report")
    ET.SubElement(report_elem, "no-step-log", {"value": "true"})
    ET.SubElement(report_elem, "duration-log.disable", {"value": "true"})
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(output_file, encoding="utf-8", xml_declaration=True)


def generate_for_candidates(
    candidates_csv: str | Path,
    nets_dir: str | Path = "sumo_nets",
    output_dir: str | Path = "outputs",
    seeds: tuple[int, ...] = (42, 43, 44),
    sim_duration: int = 1800,
    warmup: int = 300,
    step_length: float = 1.0,
    demand_profile: str = "average",
) -> pd.DataFrame:
    candidates = pd.read_csv(candidates_csv)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    failures = []
    for _, row in candidates.iterrows():
        cw_id = row["횡단보도ID"]
        cw_dir = Path(nets_dir) / f"cw_{cw_id}"
        net_file = cw_dir / "network.net.xml"
        metadata_file = cw_dir / "metadata.json"
        try:
            metadata = load_metadata(metadata_file)
        except Exception as exc:
            failures.append({"횡단보도ID": cw_id, "step": "load_metadata", "error": str(exc)})
            continue

        for seed in seeds:
            print(f"[demand] cw_{cw_id} seed={seed} profile={demand_profile}", flush=True)
            params = get_demand_params(row, seed, demand_profile)
            vehicle_file = cw_dir / f"routes_seed{seed}.rou.xml"
            pedestrian_file = cw_dir / f"peds_seed{seed}.rou.xml"
            try:
                generate_vehicle_routes(params, net_file, vehicle_file, sim_duration, seed)
                ped_count = generate_pedestrian_demand(
                    params,
                    metadata["ped_route"],
                    pedestrian_file,
                    sim_duration,
                    seed,
                )
                for scenario in ("baseline", "smart"):
                    write_sumocfg(
                        net_file,
                        vehicle_file,
                        pedestrian_file,
                        cw_dir / f"{scenario}_seed{seed}.sumocfg",
                        sim_duration,
                        warmup,
                        step_length,
                    )
                rows.append(
                    {
                        "횡단보도ID": cw_id,
                        "seed": seed,
                        "demand_profile": params["demand_profile"],
                        "veh_per_hour": params["veh_per_hour"],
                        "ped_lambda": params["ped_lambda"],
                        "elderly_ratio": params["elderly_ratio"],
                        "ped_count": ped_count,
                    }
                )
            except Exception as exc:
                failures.append({"횡단보도ID": cw_id, "seed": seed, "step": "generate_demand", "error": str(exc)})

    demand_df = pd.DataFrame(rows)
    demand_df.to_csv(output_dir / "demand_params.csv", index=False)
    if failures:
        failed_path = output_dir / "failed_cases.csv"
        pd.DataFrame(failures).to_csv(
            failed_path,
            mode="a",
            header=not failed_path.exists(),
            index=False,
        )
    return demand_df


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", default="outputs/candidates.csv")
    parser.add_argument("--nets_dir", default="sumo_nets")
    parser.add_argument("--output_dir", default="outputs")
    parser.add_argument("--seeds", nargs="+", type=int, default=[42, 43, 44])
    parser.add_argument("--sim_duration", type=int, default=1800)
    parser.add_argument("--warmup", type=int, default=300)
    parser.add_argument("--step_length", type=float, default=1.0)
    parser.add_argument("--demand_profile", choices=sorted(DEMAND_PROFILES), default="average")
    args = parser.parse_args()
    generate_for_candidates(
        args.candidates,
        args.nets_dir,
        args.output_dir,
        tuple(args.seeds),
        args.sim_duration,
        args.warmup,
        args.step_length,
        args.demand_profile,
    )


if __name__ == "__main__":
    main()
