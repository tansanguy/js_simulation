from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from .network_utils import load_metadata
    from .run_simulations import compute_signal_timing, run_simulation
except ImportError:
    from network_utils import load_metadata
    from run_simulations import compute_signal_timing, run_simulation


def mean_or_nan(values: list[float]) -> float:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0 or np.all(np.isnan(arr)):
        return float("nan")
    return float(np.nanmean(arr))


def collect_all(
    candidates_csv: str | Path,
    output_dir: str | Path = "outputs",
    nets_dir: str | Path = "sumo_nets",
    sim_duration: int = 1800,
    warmup: int = 300,
    seeds: tuple[int, ...] = (42, 43, 44),
    traci_step_length: float = 0.1,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates = pd.read_csv(candidates_csv)
    seed_rows = []
    avg_rows = []
    failures = []

    for _, row in candidates.iterrows():
        cw_id = row["횡단보도ID"]
        cw_dir = Path(nets_dir) / f"cw_{cw_id}"
        net_file = cw_dir / "network.net.xml"
        metadata_file = cw_dir / "metadata.json"
        try:
            metadata = load_metadata(metadata_file)
        except Exception as exc:
            failures.append({"횡단보도ID": cw_id, "step": "collect_load_metadata", "error": str(exc)})
            continue

        signal_timing = compute_signal_timing(row)
        cw_params = {"crossing_length_m": float(row["crossing_length_m"])}

        for scenario in ("baseline", "smart"):
            scenario_seed_rows = []
            for seed in seeds:
                route_file = cw_dir / f"routes_seed{seed}.rou.xml"
                ped_file = cw_dir / f"peds_seed{seed}.rou.xml"
                sumocfg = cw_dir / f"{scenario}_seed{seed}.sumocfg"
                try:
                    metrics = run_simulation(
                        net_file,
                        route_file,
                        ped_file,
                        sumocfg,
                        scenario,
                        signal_timing,
                        cw_params,
                        metadata,
                        sim_duration,
                        warmup,
                        seed,
                        traci_step_length,
                    )
                except Exception as exc:
                    failures.append(
                        {
                            "횡단보도ID": cw_id,
                            "scenario": scenario,
                            "seed": seed,
                            "step": "run_simulation",
                            "error": str(exc),
                        }
                    )
                    continue

                seed_row = {
                    "횡단보도ID": cw_id,
                    "seed": seed,
                    "행정동": row["행정동"],
                    "읍면동명": row["읍면동명"],
                    "시나리오": scenario,
                    "사고건수_원본": row["사고건수"],
                    "노인비율": row["노인비율"],
                    "LANES": row["LANES"],
                    "MAX_SPD": row["MAX_SPD"],
                    "crossing_length_m": row["crossing_length_m"],
                    "ped_green_base_sec": signal_timing["ped_green"],
                    "risk_score": row["risk_score"],
                    **metrics,
                }
                seed_rows.append(seed_row)
                scenario_seed_rows.append(seed_row)

            if scenario_seed_rows:
                metric_keys = [
                    key
                    for key in scenario_seed_rows[0]
                    if key
                    not in {
                        "횡단보도ID",
                        "seed",
                        "행정동",
                        "읍면동명",
                        "시나리오",
                    }
                    and pd.api.types.is_numeric_dtype(pd.Series([scenario_seed_rows[0][key]]))
                ]
                avg_metrics = {
                    key: mean_or_nan([seed_row.get(key, np.nan) for seed_row in scenario_seed_rows])
                    for key in metric_keys
                }
                avg_rows.append(
                    {
                        "횡단보도ID": cw_id,
                        "행정동": row["행정동"],
                        "읍면동명": row["읍면동명"],
                        "시나리오": scenario,
                        **avg_metrics,
                    }
                )

    seed_df = pd.DataFrame(seed_rows)
    avg_df = pd.DataFrame(avg_rows)
    seed_df.to_csv(output_dir / "simulation_results_seed.csv", index=False)
    avg_df.to_csv(output_dir / "simulation_results.csv", index=False)
    if failures:
        failed_path = output_dir / "failed_cases.csv"
        pd.DataFrame(failures).to_csv(
            failed_path,
            mode="a",
            header=not failed_path.exists(),
            index=False,
        )
    return seed_df, avg_df


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", default="outputs/candidates.csv")
    parser.add_argument("--output_dir", default="outputs")
    parser.add_argument("--nets_dir", default="sumo_nets")
    parser.add_argument("--seeds", nargs="+", type=int, default=[42, 43, 44])
    parser.add_argument("--sim_duration", type=int, default=1800)
    parser.add_argument("--warmup", type=int, default=300)
    parser.add_argument("--traci_step_length", type=float, default=0.1)
    args = parser.parse_args()
    collect_all(
        args.candidates,
        args.output_dir,
        args.nets_dir,
        args.sim_duration,
        args.warmup,
        tuple(args.seeds),
        args.traci_step_length,
    )


if __name__ == "__main__":
    main()
