from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smart_crosswalk_sumo.paired_significance_analysis import analyze


def _paired_rows(
    candidate_id: str,
    primary_pairs: list[tuple[float, float]],
    *,
    smart_extension: int,
    traffic_delta: float = 0.1,
    scenarios: tuple[str, ...] = ("baseline", "smart"),
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for seed, (baseline_primary, smart_primary) in enumerate(primary_pairs, start=1):
        for scenario, primary, extension, wait, traffic in (
            ("baseline", baseline_primary, 0, 0.0, 10.0),
            ("smart", smart_primary, smart_extension, 5.0, 10.0 + traffic_delta),
        ):
            if scenario not in scenarios:
                continue
            rows.append(
                {
                    "crosswalk_id": candidate_id,
                    "scenario": scenario,
                    "seed": seed,
                    "pedestrian_clearance_failure_count": primary,
                    "average_pedestrian_wait_time": wait,
                    "local_500m_avg_delay_sec": traffic,
                    "extension_count": extension,
                    "expected_ped_repeat_count": 5,
                    "pedestrian_crossing_count": 5 - primary,
                    "vehicle_route_sha256": f"veh-{seed}",
                    "pedestrian_route_sha256": f"ped-{seed}",
                    "_route_demand_manifest_exists": True,
                    "network_teleport_count": 0,
                    "network_collision_count": 0,
                }
            )
    return pd.DataFrame(rows)


def test_n5_clearance_improvement_keeps_even_when_wait_gets_worse() -> None:
    seed_df = _paired_rows("NODE_PASS", [(3, 1), (3, 1), (4, 1), (3, 1), (4, 1)], smart_extension=1)

    summary = analyze(
        seed_df=seed_df,
        candidate_ids=["NODE_PASS"],
        checkpoints=[1, 5, 10],
        boundary=0.0125,
    )

    latest = summary[summary["checkpoint"] == 5].iloc[0]
    assert latest["primary_metric"] == "pedestrian_clearance_failure_delta"
    assert latest["primary_improvement_mean"] > 0
    assert latest["pedestrian_wait_improvement"] < 0
    assert latest["pass_cut_keep_recheck"] == "KEEP"


def test_n12_graduation_passes_strong_clearance_candidate() -> None:
    seed_df = _paired_rows(
        "NODE_PASS",
        [(5, 2), (5, 2), (5, 1), (5, 2), (5, 2), (5, 1), (5, 2), (5, 2), (5, 1), (5, 2), (5, 2), (5, 1)],
        smart_extension=1,
    )

    summary = analyze(
        seed_df=seed_df,
        candidate_ids=["NODE_PASS"],
        checkpoints=[1, 5, 10, 12],
        boundary=0.0125,
    )

    latest = summary[summary["checkpoint"] == 12].iloc[0]
    assert latest["pass_cut_keep_recheck"] == "PASS"
    assert latest["graduation_round"] == "n12"
    assert latest["eligible_for_graduation"] is True or bool(latest["eligible_for_graduation"])


def test_no_smart_extension_by_n5_is_cut() -> None:
    seed_df = _paired_rows("NODE_NO_EXT", [(3, 3), (3, 3), (4, 4), (3, 3), (4, 4)], smart_extension=0)

    summary = analyze(
        seed_df=seed_df,
        candidate_ids=["NODE_NO_EXT"],
        checkpoints=[1, 3, 5],
        boundary=0.0125,
    )

    latest = summary[summary["checkpoint"] == 5].iloc[0]
    assert latest["pass_cut_keep_recheck"] == "CUT"
    assert latest["stop_reason"] == "no_smart_extension_seen_by_n5"


def test_n5_bottom_ten_percent_tradeoff_cutoff() -> None:
    frames = []
    candidate_ids = []
    for idx in range(10):
        candidate_id = f"NODE_{idx}"
        candidate_ids.append(candidate_id)
        frames.append(
            _paired_rows(
                candidate_id,
                [(5, 4), (5, 4), (5, 3), (5, 4), (5, 4)],
                smart_extension=1,
                traffic_delta=0.1 + idx,
            )
        )
    seed_df = pd.concat(frames, ignore_index=True)

    summary = analyze(
        seed_df=seed_df,
        candidate_ids=candidate_ids,
        checkpoints=[1, 5],
        boundary=0.0125,
    )

    latest = summary[summary["checkpoint"] == 5]
    cut = latest[latest["pass_cut_keep_recheck"] == "CUT"]
    assert len(cut) == 1
    assert cut.iloc[0]["stop_reason"] == "tradeoff_bottom_cutoff_n5"
    assert cut.iloc[0]["cut_applied"] is True or bool(cut.iloc[0]["cut_applied"])


def test_clearance_all_nan_falls_back_to_unfinished_crossing() -> None:
    seed_df = _paired_rows("NODE_FALLBACK", [(3, 1), (3, 1), (4, 1), (3, 1), (4, 1)], smart_extension=1)
    seed_df["pedestrian_clearance_failure_count"] = np.nan
    seed_df["unfinished_crossing_count"] = seed_df.apply(
        lambda row: 3 if row["scenario"] == "baseline" else 1,
        axis=1,
    )

    summary = analyze(
        seed_df=seed_df,
        candidate_ids=["NODE_FALLBACK"],
        checkpoints=[1, 5],
        boundary=0.0125,
    )

    latest = summary[summary["checkpoint"] == 5].iloc[0]
    assert latest["primary_metric"] == "pedestrian_clearance_failure_delta"
    assert latest["primary_metric_source"] == "unfinished_crossing_count"
    assert latest["primary_improvement_mean"] > 0
    assert latest["pass_cut_keep_recheck"] == "KEEP"


def test_missing_route_hashes_are_recheck() -> None:
    seed_df = _paired_rows("NODE_HASH_MISSING", [(3, 1)], smart_extension=1)
    seed_df = seed_df.drop(columns=["vehicle_route_sha256", "pedestrian_route_sha256"])

    summary = analyze(
        seed_df=seed_df,
        candidate_ids=["NODE_HASH_MISSING"],
        checkpoints=[1],
        boundary=0.0125,
    )

    latest = summary.iloc[0]
    assert latest["pass_cut_keep_recheck"] == "RECHECK"
    assert "vehicle_route_hash_missing" in latest["quality_reasons"]
    assert "pedestrian_route_hash_missing" in latest["quality_reasons"]


def test_missing_route_demand_manifest_is_recheck() -> None:
    seed_df = _paired_rows("NODE_MANIFEST_MISSING", [(3, 1)], smart_extension=1)
    seed_df["_route_demand_manifest_exists"] = False

    summary = analyze(
        seed_df=seed_df,
        candidate_ids=["NODE_MANIFEST_MISSING"],
        checkpoints=[1],
        boundary=0.0125,
    )

    latest = summary.iloc[0]
    assert latest["pass_cut_keep_recheck"] == "RECHECK"
    assert "route_demand_manifest_missing" in latest["quality_reasons"]


def test_baseline_only_input_is_recheck() -> None:
    seed_df = _paired_rows("NODE_BASELINE_ONLY", [(3, 1)], smart_extension=1, scenarios=("baseline",))

    summary = analyze(
        seed_df=seed_df,
        candidate_ids=["NODE_BASELINE_ONLY"],
        checkpoints=[1],
        boundary=0.0125,
    )

    latest = summary.iloc[0]
    assert latest["pass_cut_keep_recheck"] == "RECHECK"
    assert "baseline_only_input" in latest["quality_reasons"]
    assert "missing_baseline_smart_pair" in latest["quality_reasons"]


def test_smart_only_input_is_recheck() -> None:
    seed_df = _paired_rows("NODE_SMART_ONLY", [(3, 1)], smart_extension=1, scenarios=("smart",))

    summary = analyze(
        seed_df=seed_df,
        candidate_ids=["NODE_SMART_ONLY"],
        checkpoints=[1],
        boundary=0.0125,
    )

    latest = summary.iloc[0]
    assert latest["pass_cut_keep_recheck"] == "RECHECK"
    assert "smart_only_input" in latest["quality_reasons"]
    assert "missing_baseline_smart_pair" in latest["quality_reasons"]


def test_complete_pair_only_allows_keep_at_smoke_checkpoint() -> None:
    seed_df = _paired_rows("NODE_COMPLETE_PAIR", [(3, 1)], smart_extension=1)

    summary = analyze(
        seed_df=seed_df,
        candidate_ids=["NODE_COMPLETE_PAIR"],
        checkpoints=[1],
        boundary=0.0125,
    )

    latest = summary.iloc[0]
    assert latest["pass_cut_keep_recheck"] == "KEEP"
    assert latest["complete_pair_count"] == 1
