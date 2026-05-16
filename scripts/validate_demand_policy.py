from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from smart_crosswalk_sumo.demand_validation import validate_demand_run  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate generated pedestrian and vehicle demand outputs.")
    parser.add_argument("--run_dir", required=True, help="Path to result/<run_name> directory.")
    parser.add_argument("--output_dir", default=None, help="Optional explicit outputs directory.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    outputs = validate_demand_run(run_dir=args.run_dir, output_dir=args.output_dir)
    for key, value in outputs.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
