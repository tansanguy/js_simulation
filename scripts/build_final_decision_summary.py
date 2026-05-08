#!/usr/bin/env python3
from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from smart_crosswalk_sumo.final_decision_summary import main


if __name__ == "__main__":
    main()
