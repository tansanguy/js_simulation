#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from smart_crosswalk_sumo.vehicle_demand_policy import build_vehicle_demand_audit


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output_dir", default="outputs")
    parser.add_argument("--demand_csv", default=None)
    parser.add_argument("--scenario_name", default="main_realistic_stress")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    demand_csv = Path(args.demand_csv) if args.demand_csv else output_dir / "demand_params.csv"
    if not demand_csv.exists():
        raise FileNotFoundError(f"missing demand csv: {demand_csv}")
    demand_df = pd.read_csv(demand_csv)
    paths = build_vehicle_demand_audit(
        output_dir,
        demand_df,
        scenario_name=args.scenario_name,
        legacy_notes=[
            "This standalone audit script analyzes the generated demand CSV and route files only.",
        ],
    )
    for key, path in paths.items():
        print(f"{key}: {path}")


if __name__ == "__main__":
    main()
