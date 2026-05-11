from __future__ import annotations

import argparse
from pathlib import Path

try:
    from .mpl_runtime import configure_matplotlib, ensure_matplotlib_env
except ImportError:
    from mpl_runtime import configure_matplotlib, ensure_matplotlib_env

ensure_matplotlib_env()

import matplotlib

matplotlib.use("Agg")
configure_matplotlib(matplotlib)
import matplotlib.pyplot as plt
import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser(description="Vehicle sensitivity CSV plotting helper")
    parser.add_argument("--input_csv", required=True)
    parser.add_argument("--output_dir", required=True)
    args = parser.parse_args()

    df = pd.read_csv(args.input_csv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    delay = (
        df.pivot_table(
            index="traffic_level",
            columns=["disruption_scenario", "signal_policy"],
            values="average_vehicle_delay",
        )
        .sort_index()
    )
    delay.plot(kind="bar", figsize=(14, 6))
    plt.ylabel("Average Vehicle Delay (sec)")
    plt.tight_layout()
    plt.savefig(output_dir / "average_vehicle_delay_by_disruption.png", dpi=180)
    plt.close()

    queue = (
        df.pivot_table(
            index="traffic_level",
            columns=["disruption_scenario", "signal_policy"],
            values="max_vehicle_queue_length",
        )
        .sort_index()
    )
    queue.plot(kind="bar", figsize=(14, 6))
    plt.ylabel("Max Vehicle Queue Length")
    plt.tight_layout()
    plt.savefig(output_dir / "max_vehicle_queue_by_disruption.png", dpi=180)
    plt.close()

    subset = df[df["disruption_scenario"].isin(["best_case", "congested_urban"])].copy()
    cost = (
        subset.pivot_table(
            index="traffic_level",
            columns=["disruption_scenario", "signal_policy"],
            values="weighted_cost",
        )
        .sort_index()
    )
    cost.plot(marker="o", figsize=(12, 5))
    plt.ylabel("Weighted Cost")
    plt.tight_layout()
    plt.savefig(output_dir / "weighted_cost_best_vs_congested.png", dpi=180)
    plt.close()

    smart_only = df[df["signal_policy"] == "smart_crosswalk"].copy()
    smart_only = smart_only.sort_values(["disruption_scenario", "vehicle_arrival_rate_per_hour"])
    for disruption_scenario, group in smart_only.groupby("disruption_scenario"):
        plt.plot(
            group["vehicle_arrival_rate_per_hour"],
            group["average_vehicle_delay"],
            marker="o",
            label=disruption_scenario,
        )
    plt.xlabel("Vehicle Arrival Rate per Hour")
    plt.ylabel("Smart Crosswalk Average Vehicle Delay")
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_dir / "smart_delay_vs_traffic_level.png", dpi=180)
    plt.close()


if __name__ == "__main__":
    main()
