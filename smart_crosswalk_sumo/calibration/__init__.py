from __future__ import annotations

import importlib.util
from pathlib import Path

# smart_crosswalk_sumo/calibration.py is shadowed by this package directory.
# Load it directly via importlib so main.py can still import calibrate.
_legacy_path = Path(__file__).resolve().parent.parent / "calibration.py"
_spec = importlib.util.spec_from_file_location(
    "smart_crosswalk_sumo._calibration_legacy", _legacy_path
)
_legacy_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_legacy_mod)  # type: ignore[union-attr]

calibrate = _legacy_mod.calibrate
