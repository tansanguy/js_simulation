#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pandas.errors import EmptyDataError
from scipy import stats  # type: ignore

try:
    from .output_schema import write_csv_utf8_sig
except ImportError:
    from output_schema import write_csv_utf8_sig


CUT_ROUNDS = {5: 0.10, 10: 0.20}
GRADUATION_ROUNDS = (12, 15, 18, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30)
DEFAULT_CHECKPOINTS = (1, 5, 10, *GRADUATION_ROUNDS)
PRIMARY_METRIC = "pedestrian_clearance_failure_delta"
PRIMARY_VALUE_COLUMNS = (
    "pedestrian_clearance_failure_count",
    "unfinished_crossing_count",
    "low_pet_event_count",
    "very_risky_crossing_count",
    "risky_crossing_count",
    "accident_risk_estimate",
    "safety_risk_score",
)
WAIT_VALUE_COLUMNS = (
    "average_pedestrian_wait_time",
    "pedestrian_waiting_time_mean",
    "ped_wait_time_mean",
)
EXTENSION_VALUE_COLUMNS = (
    "extension_count",
    "pedestrian_green_extension_count",
)
LOW_PET_VALUE_COLUMNS = (
    "low_pet_event_count",
    "very_risky_crossing_count",
    "risky_crossing_count",
)
TRAFFIC_VALUE_COLUMNS = (
    "local_500m_avg_delay_sec",
    "veh_avg_delay_sec",
    "network_mean_time_loss",
    "surrounding_road_delay_sec",
    "avg_vehicle_delay_sec",
    "vehicle_delay_cost",
)


def _parse_checkpoints(text: str) -> list[int]:
    checkpoints: list[int] = []
    for token in str(text).replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        value = int(token)
        if value <= 0:
            raise ValueError(f"checkpoint must be positive: {value}")
        checkpoints.append(value)
    if not checkpoints:
        raise ValueError("at least one checkpoint is required")
    return sorted(dict.fromkeys(checkpoints))


def _parse_statuses(text: str) -> set[str]:
    values = {token.strip().upper() for token in str(text).replace(";", ",").split(",") if token.strip()}
    return values or {"KEEP"}


def _seed_sort_key(value: Any) -> tuple[int, str]:
    text = str(value)
    try:
        return int(float(text)), text
    except Exception:
        return sys.maxsize, text


def _first_existing_seed_csv(path: Path) -> Path | None:
    if path.is_file():
        return path
    candidates = (
        path / "simulation_results_seed.csv",
        path / "simulation_result.csv",
        path / "csv" / "results" / "simulation_results_seed.csv",
        path / "csv" / "results" / "simulation_result.csv",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _expand_inputs(input_dirs: list[str], run_globs: list[str]) -> list[Path]:
    paths: list[Path] = []
    for item in input_dirs:
        paths.append(Path(item).expanduser())
    for pattern in run_globs:
        matches = glob.glob(str(Path(pattern).expanduser()))
        paths.extend(Path(match) for match in sorted(matches))

    resolved: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        csv_path = _first_existing_seed_csv(path)
        if csv_path is None:
            continue
        key = str(csv_path.resolve())
        if key in seen:
            continue
        seen.add(key)
        resolved.append(csv_path.resolve())
    return resolved


def _failed_cases_count(seed_csv: Path) -> int:
    failed_path = seed_csv.parent / "failed_cases.csv"
    if not failed_path.exists():
        return 0
    try:
        failed = pd.read_csv(failed_path, encoding="utf-8-sig")
    except (EmptyDataError, Exception):
        return 0
    if failed.empty:
        return 0
    if "error" in failed.columns:
        errors = failed["error"].fillna("").astype(str).str.strip()
        return int((errors != "").sum())
    return int(len(failed.dropna(how="all")))


def _read_seed_csv(path: Path) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path, encoding="utf-8-sig")
    except EmptyDataError:
        frame = pd.DataFrame()
    if frame.empty:
        return frame
    frame = frame.copy()
    frame["source_file"] = str(path)
    frame["source_dir"] = str(path.parent)
    frame["_failed_cases_count"] = _failed_cases_count(path)
    frame["_route_demand_manifest_exists"] = (path.parent / "route_demand_manifest.csv").exists()
    return frame


def _load_seed_results(paths: list[Path]) -> pd.DataFrame:
    frames = [_read_seed_csv(path) for path in paths]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    required = {"crosswalk_id", "scenario", "seed"}
    missing = sorted(required - set(out.columns))
    if missing:
        raise ValueError(f"seed result missing required columns: {missing}")
    out["candidate_id"] = out["crosswalk_id"].astype(str)
    out["scenario"] = out["scenario"].astype(str).replace({"smart_selected": "smart"})
    out["seed"] = pd.to_numeric(out["seed"], errors="coerce")
    out = out[out["seed"].notna()].copy()
    out["seed"] = out["seed"].astype(int)
    return out


def _load_candidate_ids(candidate_csv: str | None, seed_df: pd.DataFrame) -> list[str]:
    if candidate_csv:
        candidates = pd.read_csv(Path(candidate_csv).expanduser(), encoding="utf-8-sig")
        if "crosswalk_id" not in candidates.columns:
            raise ValueError(f"candidate csv missing crosswalk_id: {candidate_csv}")
        ids = [str(value) for value in candidates["crosswalk_id"].dropna().tolist()]
        return list(dict.fromkeys(ids))
    if seed_df.empty:
        return []
    ids = [str(value) for value in seed_df["candidate_id"].dropna().tolist()]
    return sorted(dict.fromkeys(ids))


def _first_existing_column(frame: pd.DataFrame, columns: tuple[str, ...], label: str, *, required: bool) -> str | None:
    for column in columns:
        if column in frame.columns:
            return column
    if required:
        raise ValueError(f"{label} requires one of: {', '.join(columns)}")
    return None


def _first_numeric_column_with_data(
    frame: pd.DataFrame,
    columns: tuple[str, ...],
    label: str,
    *,
    required: bool,
) -> str | None:
    existing = [column for column in columns if column in frame.columns]
    for column in existing:
        if pd.to_numeric(frame[column], errors="coerce").notna().any():
            return column
    if existing:
        return existing[0]
    if required:
        raise ValueError(f"{label} requires one of: {', '.join(columns)}")
    return None


def _numeric_or_nan(frame: pd.DataFrame, column: str | None) -> pd.Series:
    if column is None or column not in frame.columns:
        return pd.Series([np.nan] * len(frame), index=frame.index, dtype="float64")
    return pd.to_numeric(frame[column], errors="coerce")


def _coalesced_numeric_or_nan(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.Series:
    out = pd.Series([np.nan] * len(frame), index=frame.index, dtype="float64")
    for column in columns:
        if column not in frame.columns:
            continue
        values = pd.to_numeric(frame[column], errors="coerce")
        out = out.where(out.notna(), values)
    return out


def _first_text(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series([""] * len(frame), index=frame.index, dtype="object")
    return frame[column].fillna("").astype(str)


def _metric_source_columns(primary_metric: str) -> tuple[str, ...]:
    if primary_metric == "pedestrian_wait_delta":
        return WAIT_VALUE_COLUMNS
    if primary_metric in {"low_pet_event_count_delta", "very_risky_crossing_count_delta", "risky_crossing_count_delta"}:
        return (primary_metric.removesuffix("_delta"),)
    if primary_metric == "unfinished_crossing_delta":
        return ("unfinished_crossing_count", "pedestrian_clearance_failure_count")
    if primary_metric == "pedestrian_clearance_failure_delta":
        return PRIMARY_VALUE_COLUMNS
    raise ValueError(f"unsupported primary metric: {primary_metric}")


def _build_seed_pairs(
    seed_df: pd.DataFrame,
    candidate_ids: list[str],
    primary_metric: str,
) -> tuple[pd.DataFrame, dict[str, list[str]], dict[str, str]]:
    if "candidate_id" not in seed_df.columns:
        seed_df = seed_df.copy()
        seed_df["candidate_id"] = seed_df["crosswalk_id"].astype(str)
    primary_source_columns = _metric_source_columns(primary_metric)
    primary_col = _first_numeric_column_with_data(
        seed_df,
        primary_source_columns,
        primary_metric,
        required=True,
    )
    wait_col = _first_existing_column(seed_df, WAIT_VALUE_COLUMNS, "pedestrian_wait_delta", required=False)
    traffic_col = _first_existing_column(seed_df, TRAFFIC_VALUE_COLUMNS, "vehicle_delay_cost", required=True)
    safety_col = _first_existing_column(seed_df, LOW_PET_VALUE_COLUMNS, "safety_improvement", required=False)
    extension_col = _first_existing_column(seed_df, EXTENSION_VALUE_COLUMNS, "extension_count", required=False)

    frame = seed_df[seed_df["scenario"].isin(["baseline", "smart"])].copy()
    frame = frame[frame["candidate_id"].isin(candidate_ids)].copy()
    frame["primary_value"] = _coalesced_numeric_or_nan(frame, primary_source_columns)
    frame["wait_value"] = _numeric_or_nan(frame, wait_col)
    frame["traffic_value"] = _numeric_or_nan(frame, traffic_col)
    frame["safety_value"] = _numeric_or_nan(frame, safety_col)
    frame["extension_value"] = _numeric_or_nan(frame, extension_col).fillna(0)
    frame["expected_ped_repeat_count"] = _numeric_or_nan(frame, "expected_ped_repeat_count")
    frame["pedestrian_crossing_count"] = _numeric_or_nan(frame, "pedestrian_crossing_count")
    frame["network_teleport_count"] = _numeric_or_nan(frame, "network_teleport_count").fillna(0)
    frame["network_collision_count"] = _numeric_or_nan(frame, "network_collision_count").fillna(0)
    frame["vehicle_route_sha256"] = _first_text(frame, "vehicle_route_sha256")
    frame["pedestrian_route_sha256"] = _first_text(frame, "pedestrian_route_sha256")
    if "_route_demand_manifest_exists" in frame.columns:
        frame["_route_demand_manifest_exists"] = pd.to_numeric(
            frame["_route_demand_manifest_exists"], errors="coerce"
        ).fillna(0).astype(bool)
    else:
        frame["_route_demand_manifest_exists"] = False
    if "_failed_cases_count" in frame.columns:
        frame["_failed_cases_count"] = pd.to_numeric(frame["_failed_cases_count"], errors="coerce").fillna(0)
    else:
        frame["_failed_cases_count"] = 0

    duplicate_mask = frame.duplicated(["candidate_id", "seed", "scenario"], keep=False)
    duplicate_counts = (
        frame[duplicate_mask].groupby("candidate_id").size().astype(int).to_dict()
        if duplicate_mask.any()
        else {}
    )

    grouped = (
        frame.groupby(["candidate_id", "seed", "scenario"], as_index=False)
        .agg(
            wait_value=("wait_value", "mean"),
            primary_value=("primary_value", "mean"),
            traffic_value=("traffic_value", "mean"),
            safety_value=("safety_value", "mean"),
            extension_value=("extension_value", "sum"),
            expected_ped_repeat_count=("expected_ped_repeat_count", "mean"),
            pedestrian_crossing_count=("pedestrian_crossing_count", "mean"),
            network_teleport_count=("network_teleport_count", "sum"),
            network_collision_count=("network_collision_count", "sum"),
            vehicle_route_sha256=("vehicle_route_sha256", "first"),
            pedestrian_route_sha256=("pedestrian_route_sha256", "first"),
            route_demand_manifest_exists=("_route_demand_manifest_exists", "max"),
            failed_cases_count=("_failed_cases_count", "max"),
        )
    )

    base_keys = ["candidate_id", "seed"]
    base = grouped[grouped["scenario"] == "baseline"].drop(columns=["scenario"]).rename(
        columns={
            "wait_value": "wait_baseline",
            "primary_value": "primary_baseline",
            "traffic_value": "traffic_baseline",
            "safety_value": "safety_baseline",
            "extension_value": "extension_baseline",
            "expected_ped_repeat_count": "expected_ped_repeat_count_baseline",
            "pedestrian_crossing_count": "pedestrian_crossing_count_baseline",
            "network_teleport_count": "network_teleport_count_baseline",
            "network_collision_count": "network_collision_count_baseline",
            "vehicle_route_sha256": "vehicle_route_sha256_baseline",
            "pedestrian_route_sha256": "pedestrian_route_sha256_baseline",
            "route_demand_manifest_exists": "route_demand_manifest_exists_baseline",
            "failed_cases_count": "failed_cases_count_baseline",
        }
    )
    smart = grouped[grouped["scenario"] == "smart"].drop(columns=["scenario"]).rename(
        columns={
            "wait_value": "wait_smart",
            "primary_value": "primary_smart",
            "traffic_value": "traffic_smart",
            "safety_value": "safety_smart",
            "extension_value": "extension_smart",
            "expected_ped_repeat_count": "expected_ped_repeat_count_smart",
            "pedestrian_crossing_count": "pedestrian_crossing_count_smart",
            "network_teleport_count": "network_teleport_count_smart",
            "network_collision_count": "network_collision_count_smart",
            "vehicle_route_sha256": "vehicle_route_sha256_smart",
            "pedestrian_route_sha256": "pedestrian_route_sha256_smart",
            "route_demand_manifest_exists": "route_demand_manifest_exists_smart",
            "failed_cases_count": "failed_cases_count_smart",
        }
    )
    paired = base.merge(smart, on=base_keys, how="outer")

    paired["pedestrian_wait_delta"] = paired["wait_smart"] - paired["wait_baseline"]
    paired["pedestrian_wait_improvement"] = paired["wait_baseline"] - paired["wait_smart"]
    paired[primary_metric] = paired["primary_smart"] - paired["primary_baseline"]
    paired["primary_delta"] = paired[primary_metric]
    paired["primary_improvement"] = paired["primary_baseline"] - paired["primary_smart"]
    paired["vehicle_delay_cost"] = paired["traffic_smart"] - paired["traffic_baseline"]
    paired["safety_improvement"] = paired["safety_baseline"] - paired["safety_smart"]
    paired["smart_extension_count"] = pd.to_numeric(paired.get("extension_smart"), errors="coerce").fillna(0)
    paired["baseline_extension_count"] = pd.to_numeric(paired.get("extension_baseline"), errors="coerce").fillna(0)
    paired["extension_seen"] = paired["smart_extension_count"] > 0
    paired["extension_policy_violation"] = (
        (paired["baseline_extension_count"] > 0)
        | (paired["smart_extension_count"] > 1)
    )
    demand_values = np.vstack(
        [
            _numeric_or_nan(paired, "expected_ped_repeat_count_baseline").to_numpy(dtype=float),
            _numeric_or_nan(paired, "expected_ped_repeat_count_smart").to_numpy(dtype=float),
        ]
    )
    with np.errstate(all="ignore"):
        paired["pedestrian_demand_count"] = np.nanmax(demand_values, axis=0)
    paired["teleport_delta"] = (
        _numeric_or_nan(paired, "network_teleport_count_smart").fillna(0)
        - _numeric_or_nan(paired, "network_teleport_count_baseline").fillna(0)
    )
    paired["collision_delta"] = (
        _numeric_or_nan(paired, "network_collision_count_smart").fillna(0)
        - _numeric_or_nan(paired, "network_collision_count_baseline").fillna(0)
    )
    paired["failed_cases_count"] = (
        pd.to_numeric(paired.get("failed_cases_count_baseline"), errors="coerce").fillna(0)
        + pd.to_numeric(paired.get("failed_cases_count_smart"), errors="coerce").fillna(0)
    )

    for prefix in ("vehicle_route_sha256", "pedestrian_route_sha256"):
        baseline_col = f"{prefix}_baseline"
        smart_col = f"{prefix}_smart"
        if baseline_col in paired.columns and smart_col in paired.columns:
            baseline_hash = paired[baseline_col].fillna("").astype(str)
            smart_hash = paired[smart_col].fillna("").astype(str)
            known = (baseline_hash != "") & (smart_hash != "")
            paired[f"{prefix}_match"] = np.where(known, baseline_hash == smart_hash, pd.NA)
        else:
            paired[f"{prefix}_match"] = pd.NA
    baseline_manifest_exists = pd.Series(
        paired.get("route_demand_manifest_exists_baseline", pd.Series(False, index=paired.index)),
        index=paired.index,
    ).astype("boolean").fillna(False).astype(bool)
    smart_manifest_exists = pd.Series(
        paired.get("route_demand_manifest_exists_smart", pd.Series(False, index=paired.index)),
        index=paired.index,
    ).astype("boolean").fillna(False).astype(bool)
    paired["route_demand_manifest_missing"] = ~baseline_manifest_exists | ~smart_manifest_exists

    paired = paired.sort_values(["candidate_id", "seed"], key=lambda s: s.map(_seed_sort_key))

    quality: dict[str, list[str]] = {candidate_id: [] for candidate_id in candidate_ids}
    for candidate_id in candidate_ids:
        sub = paired[paired["candidate_id"] == candidate_id]
        if sub.empty:
            quality[candidate_id].append("no_seed_rows")
            continue
        baseline_rows = int(pd.to_numeric(sub["wait_baseline"], errors="coerce").notna().sum())
        smart_rows = int(pd.to_numeric(sub["wait_smart"], errors="coerce").notna().sum())
        complete_pairs = int(
            (
                pd.to_numeric(sub["wait_baseline"], errors="coerce").notna()
                & pd.to_numeric(sub["wait_smart"], errors="coerce").notna()
            ).sum()
        )
        if baseline_rows > 0 and smart_rows == 0:
            quality[candidate_id].append("baseline_only_input")
        if smart_rows > 0 and baseline_rows == 0:
            quality[candidate_id].append("smart_only_input")
        if complete_pairs == 0:
            quality[candidate_id].append("missing_baseline_smart_pair")
        primary_missing = int(sub[["primary_baseline", "primary_smart"]].isna().any(axis=1).sum())
        traffic_missing = int(sub[["traffic_baseline", "traffic_smart"]].isna().any(axis=1).sum())
        if primary_missing:
            quality[candidate_id].append(f"missing_primary_pairs={primary_missing}")
        if traffic_missing:
            quality[candidate_id].append(f"missing_traffic_pairs={traffic_missing}")
        if bool(sub["route_demand_manifest_missing"].fillna(False).astype(bool).any()):
            quality[candidate_id].append("route_demand_manifest_missing")
        failed_cases = int(pd.to_numeric(sub["failed_cases_count"], errors="coerce").fillna(0).sum())
        if failed_cases:
            quality[candidate_id].append(f"failed_cases={failed_cases}")
        duplicate_count = int(duplicate_counts.get(candidate_id, 0))
        if duplicate_count:
            quality[candidate_id].append(f"duplicate_scenario_rows={duplicate_count}")
        for column, mismatch_reason, missing_reason in (
            ("vehicle_route_sha256_match", "vehicle_route_hash_mismatch", "vehicle_route_hash_missing"),
            ("pedestrian_route_sha256_match", "pedestrian_route_hash_mismatch", "pedestrian_route_hash_missing"),
        ):
            known = sub[column].dropna()
            if not known.empty and (~known.astype(bool)).any():
                quality[candidate_id].append(mismatch_reason)
            if sub[column].isna().any():
                quality[candidate_id].append(missing_reason)
        if (pd.to_numeric(sub["baseline_extension_count"], errors="coerce").fillna(0) > 0).any():
            quality[candidate_id].append("baseline_extension_policy_violation")
        if (pd.to_numeric(sub["smart_extension_count"], errors="coerce").fillna(0) > 1).any():
            quality[candidate_id].append("smart_extension_policy_violation")
        if (pd.to_numeric(sub["pedestrian_demand_count"], errors="coerce").fillna(0) <= 0).any():
            quality[candidate_id].append("pedestrian_demand_missing")

    sources = {
        "primary_metric_source": str(primary_col),
        "wait_metric_source": str(wait_col),
        "traffic_metric_source": str(traffic_col),
        "safety_metric_source": str(safety_col or ""),
        "extension_metric_source": str(extension_col or ""),
    }
    return paired, quality, sources


def _safe_ttest(deltas: np.ndarray) -> tuple[float, float, list[str]]:
    if deltas.size < 2:
        return float("nan"), float("nan"), ["n_lt_2"]
    sd = float(np.std(deltas, ddof=1))
    if math.isclose(sd, 0.0, abs_tol=1e-12):
        return float("nan"), float("nan"), ["zero_delta_variance"]
    result = stats.ttest_1samp(deltas, popmean=0.0, nan_policy="omit")
    return float(result.statistic), float(result.pvalue), []


def _safe_wilcoxon(deltas: np.ndarray) -> tuple[float, list[str]]:
    if deltas.size < 1:
        return float("nan"), ["wilcoxon_no_data"]
    if np.allclose(deltas, 0.0, equal_nan=False):
        return 1.0, []
    try:
        result = stats.wilcoxon(deltas, zero_method="wilcox", alternative="two-sided")
    except ValueError as exc:
        return float("nan"), [f"wilcoxon_error={exc}"]
    return float(result.pvalue), []


def _bootstrap_mean_ci(deltas: np.ndarray, *, iterations: int = 5000, seed: int = 20260518) -> tuple[float, float]:
    if deltas.size < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    samples = rng.choice(deltas, size=(iterations, deltas.size), replace=True)
    means = samples.mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def _next_checkpoint(checkpoints: list[int], checkpoint: int) -> str:
    for next_value in checkpoints:
        if next_value > checkpoint:
            return str(next_value)
    return ""


def _mean_or_nan(values: pd.Series) -> float:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    return float(numeric.mean()) if not numeric.empty else float("nan")


def _sd_or_nan(values: np.ndarray) -> float:
    return float(np.std(values, ddof=1)) if values.size > 1 else float("nan")


def _zscore(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    sd = float(numeric.std(ddof=0)) if numeric.notna().sum() > 1 else float("nan")
    if pd.isna(sd) or math.isclose(sd, 0.0, abs_tol=1e-12):
        return pd.Series([np.nan] * len(series), index=series.index, dtype="float64")
    return (numeric - float(numeric.mean())) / sd


def _rank_score(frame: pd.DataFrame, traffic_cost_weight: float) -> pd.Series:
    primary_rank = frame["primary_improvement_mean"].rank(ascending=False, na_option="bottom")
    safety_rank = frame["safety_improvement"].rank(ascending=False, na_option="bottom")
    extension_rank = frame["extension_trigger_rate"].rank(ascending=False, na_option="bottom")
    traffic_rank = frame["vehicle_delay_cost"].rank(ascending=True, na_option="bottom")
    wait_rank = frame["pedestrian_wait_improvement"].rank(ascending=False, na_option="bottom")
    return -(primary_rank + safety_rank + (0.5 * extension_rank) + (traffic_cost_weight * traffic_rank) + (0.1 * wait_rank))


def _add_tradeoff_and_gate(
    result: pd.DataFrame,
    *,
    traffic_cost_weight: float,
    traffic_bad_quantile: float,
    traffic_cost_max: float | None,
) -> pd.DataFrame:
    out = result.copy()
    out["tradeoff_score"] = np.nan
    out["traffic_bad_threshold"] = np.nan
    out["traffic_gate_pass"] = False
    for checkpoint, idx in out.groupby("checkpoint").groups.items():
        group = out.loc[idx].copy()
        rank_score = _rank_score(group, traffic_cost_weight)
        out.loc[idx, "tradeoff_rank_score"] = rank_score
        primary_z = _zscore(group["primary_improvement_mean"])
        safety_z = _zscore(group["safety_improvement"])
        extension_z = _zscore(group["extension_trigger_rate"])
        traffic_z = _zscore(group["vehicle_delay_cost"])
        score = (
            primary_z.fillna(0.0)
            + safety_z.fillna(0.0)
            + (0.5 * extension_z.fillna(0.0))
            - (traffic_cost_weight * traffic_z.fillna(0.0))
        )
        out.loc[idx, "tradeoff_z_score"] = score
        if len(group) < 15 or score.nunique(dropna=True) <= 1:
            score = _rank_score(group, traffic_cost_weight)
        out.loc[idx, "tradeoff_score"] = score

        traffic_values = pd.to_numeric(group["vehicle_delay_cost"], errors="coerce").dropna()
        if traffic_values.empty:
            threshold = float("nan")
        else:
            threshold = float(traffic_values.quantile(float(traffic_bad_quantile)))
        if traffic_cost_max is not None:
            threshold = min(threshold, float(traffic_cost_max)) if pd.notna(threshold) else float(traffic_cost_max)
        out.loc[idx, "traffic_bad_threshold"] = threshold
        costs = pd.to_numeric(group["vehicle_delay_cost"], errors="coerce")
        gate = costs.notna()
        if pd.notna(threshold):
            gate &= costs <= threshold
        if traffic_cost_max is not None:
            gate &= costs <= float(traffic_cost_max)
        out.loc[idx, "traffic_gate_pass"] = gate.to_numpy()
    return out


def _add_cutoff_metadata(result: pd.DataFrame) -> pd.DataFrame:
    out = result.copy()
    out["cut_round"] = ""
    out["cut_rank"] = np.nan
    out["cut_percentile"] = np.nan
    out["cut_threshold_percentile"] = np.nan
    out["cut_margin"] = np.nan
    out["cut_applied"] = False

    for checkpoint, ratio in CUT_ROUNDS.items():
        idx = out.index[out["checkpoint"].astype(int) == int(checkpoint)]
        if len(idx) == 0:
            continue
        group = out.loc[idx].copy()
        scores = pd.to_numeric(group["tradeoff_score"], errors="coerce")
        ranked = scores.rank(method="first", ascending=True, na_option="top")
        percentile = ranked / max(len(group), 1)
        out.loc[idx, "cut_round"] = f"n{checkpoint}"
        out.loc[idx, "cut_rank"] = ranked
        out.loc[idx, "cut_percentile"] = percentile
        out.loc[idx, "cut_threshold_percentile"] = ratio

        active = group[
            group["quality_reasons"].fillna("").astype(str).str.strip().eq("")
            & group["missing_pair_count"].fillna(0).astype(float).eq(0)
        ].copy()
        min_active = 10 if checkpoint == 5 else 6
        if len(active) < min_active:
            continue
        cut_count = int(math.floor(len(active) * ratio))
        if cut_count <= 0:
            continue
        active_scores = pd.to_numeric(active["tradeoff_score"], errors="coerce")
        active_order = active.assign(_score=active_scores).sort_values("_score", ascending=True, na_position="first")
        cut_idx = list(active_order.head(cut_count).index)
        kept_scores = active_order.iloc[cut_count:]["_score"].dropna()
        cut_scores = active_order.head(cut_count)["_score"].dropna()
        if kept_scores.empty or cut_scores.empty:
            continue
        score_iqr = float(active_scores.quantile(0.75) - active_scores.quantile(0.25))
        margin = float(kept_scores.iloc[0] - cut_scores.iloc[-1])
        out.loc[cut_idx, "cut_margin"] = margin
        if score_iqr > 0 and margin < score_iqr * 0.05:
            continue
        out.loc[cut_idx, "cut_applied"] = True
    return out


def _quality_recheck_reason(row: pd.Series) -> str:
    reasons = [part for part in str(row.get("quality_reasons", "") or "").split(";") if part]
    if reasons:
        return ";".join(dict.fromkeys(reasons))
    if int(row.get("complete_pair_count", 0) or 0) <= 0:
        return "missing_baseline_smart_pair"
    if int(row.get("missing_pair_count", 0) or 0) > 0:
        return "missing_baseline_smart_pair"
    if int(row.get("extension_policy_violation_count", 0) or 0) > 0:
        return "extension_policy_violation"
    return ""


def _hard_cut_reason(row: pd.Series) -> str:
    checkpoint = int(row["checkpoint"])
    if checkpoint < 5:
        return ""
    if int(row.get("extension_seen_seed_count", 0) or 0) == 0 and int(row.get("no_extension_streak", 0) or 0) >= 5:
        return "no_smart_extension_seen_by_n5"
    if int(row.get("informative_primary_seed_count", 0) or 0) == 0:
        return "uninformative_primary_metric_all_zero"
    primary_improvement = row.get("primary_improvement_mean")
    direction_rate = row.get("direction_improved_rate")
    if (
        pd.notna(primary_improvement)
        and float(primary_improvement) <= 0.0
        and pd.notna(direction_rate)
        and float(direction_rate) < 0.40
    ):
        return "no_primary_safety_improvement"
    traffic_gate_pass = bool(row.get("traffic_gate_pass", False))
    if not traffic_gate_pass and pd.notna(primary_improvement) and float(primary_improvement) <= 0.0:
        return "traffic_gate_failed_without_safety_improvement"
    return ""


def _graduation_boundary(checkpoint: int) -> tuple[float, float]:
    if checkpoint == 12:
        return 0.0125, 0.80
    if checkpoint == 15:
        return 0.015, 0.75
    if checkpoint == 18:
        return 0.020, 0.70
    if checkpoint == 20:
        return 0.025, 0.70
    if 21 <= checkpoint <= 24:
        return 0.030, 0.70
    if 25 <= checkpoint <= 29:
        return 0.035, 0.67
    if checkpoint >= 30:
        return 0.050, 0.67
    return float("nan"), float("nan")


def _eligible_for_graduation(row: pd.Series) -> bool:
    return int(row["checkpoint"]) in GRADUATION_ROUNDS


def _graduation_reason(row: pd.Series) -> str:
    checkpoint = int(row["checkpoint"])
    if not _eligible_for_graduation(row):
        return ""
    p_boundary, direction_threshold = _graduation_boundary(checkpoint)
    paired_t_p = row.get("paired_t_p")
    delta_mean = row.get("primary_delta_mean")
    traffic_gate_pass = bool(row.get("traffic_gate_pass", False))
    direction_rate = row.get("direction_improved_rate")
    bootstrap_high = row.get("bootstrap_ci_high")
    if int(row.get("extension_seen_seed_count", 0) or 0) < 1:
        return ""
    if float(row.get("baseline_extension_count_total", 0) or 0) != 0.0:
        return ""
    if int(row.get("extension_policy_violation_count", 0) or 0) != 0:
        return ""
    if (
        pd.notna(paired_t_p)
        and float(paired_t_p) < p_boundary
        and pd.notna(delta_mean)
        and float(delta_mean) < 0.0
        and pd.notna(bootstrap_high)
        and float(bootstrap_high) < 0.0
        and pd.notna(direction_rate)
        and float(direction_rate) >= direction_threshold
        and traffic_gate_pass
    ):
        return "graduation_boundary_pass"
    return ""


def _next_active_checkpoint(checkpoints: list[int], checkpoint: int) -> str:
    for next_value in checkpoints:
        if next_value > checkpoint:
            return str(next_value)
    return ""


def _bh_qvalues(pvalues: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(pvalues, errors="coerce")
    out = pd.Series(np.nan, index=pvalues.index, dtype="float64")
    valid = numeric.dropna().sort_values()
    m = len(valid)
    if m == 0:
        return out
    adjusted = valid * m / np.arange(1, m + 1)
    adjusted = np.minimum.accumulate(adjusted.iloc[::-1]).iloc[::-1].clip(upper=1.0)
    out.loc[adjusted.index] = adjusted
    return out


def _holm_pvalues(pvalues: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(pvalues, errors="coerce")
    out = pd.Series(np.nan, index=pvalues.index, dtype="float64")
    valid = numeric.dropna().sort_values()
    m = len(valid)
    if m == 0:
        return out
    adjusted_values = (m - np.arange(0, m)) * valid.to_numpy(dtype=float)
    adjusted_values = np.maximum.accumulate(adjusted_values).clip(max=1.0)
    out.loc[valid.index] = adjusted_values
    return out


def analyze(
    seed_df: pd.DataFrame,
    candidate_ids: list[str],
    checkpoints: list[int],
    boundary: float,
    *,
    primary_metric: str = PRIMARY_METRIC,
    traffic_cost_weight: float = 2.0,
    traffic_bad_quantile: float = 0.75,
    traffic_cost_max: float | None = None,
) -> pd.DataFrame:
    pairs, quality, sources = _build_seed_pairs(seed_df, candidate_ids, primary_metric)
    rows: list[dict[str, Any]] = []
    min_checkpoint = min(checkpoints)
    max_checkpoint = max(checkpoints)

    for candidate_id in candidate_ids:
        candidate_pairs = pairs[pairs["candidate_id"] == candidate_id].copy()
        candidate_pairs = candidate_pairs.sort_values("seed", key=lambda s: s.map(_seed_sort_key))
        available = len(candidate_pairs)
        checkpoints_to_report = [checkpoint for checkpoint in checkpoints if available >= checkpoint]
        if not checkpoints_to_report:
            checkpoints_to_report = [available if available else min_checkpoint]

        for checkpoint in checkpoints_to_report:
            sub = candidate_pairs.head(checkpoint) if available else candidate_pairs
            seed_values = sub["seed"].tolist() if not sub.empty else []
            deltas = pd.to_numeric(sub["primary_delta"], errors="coerce").dropna().to_numpy(dtype=float)
            t_stat, t_p, t_reasons = _safe_ttest(deltas)
            wilcoxon_p, wilcoxon_reasons = _safe_wilcoxon(deltas)
            bootstrap_low, bootstrap_high = _bootstrap_mean_ci(deltas)
            quality_reasons = list(quality[candidate_id])
            test_reasons = list(dict.fromkeys(t_reasons + wilcoxon_reasons))

            delta_sd = _sd_or_nan(deltas)
            improved_mask = pd.to_numeric(sub["primary_delta"], errors="coerce") < 0.0
            informative_mask = (
                pd.to_numeric(sub["primary_baseline"], errors="coerce").fillna(0.0)
                + pd.to_numeric(sub["primary_smart"], errors="coerce").fillna(0.0)
            ) > 0.0
            extension_seen_seed_count = int(pd.to_numeric(sub["smart_extension_count"], errors="coerce").fillna(0).gt(0).sum())
            baseline_extension_count_total = float(pd.to_numeric(sub["baseline_extension_count"], errors="coerce").fillna(0).sum())
            smart_extension_count_total = float(pd.to_numeric(sub["smart_extension_count"], errors="coerce").fillna(0).sum())
            no_extension_streak = 0
            for value in reversed(pd.to_numeric(sub["smart_extension_count"], errors="coerce").fillna(0).tolist()):
                if float(value) > 0:
                    break
                no_extension_streak += 1
            extension_violation_count = int(pd.to_numeric(sub["extension_policy_violation"], errors="coerce").fillna(0).sum())
            baseline_rows = int(pd.to_numeric(sub["wait_baseline"], errors="coerce").notna().sum())
            smart_rows = int(pd.to_numeric(sub["wait_smart"], errors="coerce").notna().sum())
            complete_pairs = int(
                (
                    pd.to_numeric(sub["wait_baseline"], errors="coerce").notna()
                    & pd.to_numeric(sub["wait_smart"], errors="coerce").notna()
                ).sum()
            )
            traffic_bad_reasons: list[str] = []
            if pd.to_numeric(sub["collision_delta"], errors="coerce").fillna(0).gt(0).any():
                traffic_bad_reasons.append("collision_count_increased")
            if pd.to_numeric(sub["teleport_delta"], errors="coerce").fillna(0).gt(0).any():
                traffic_bad_reasons.append("teleport_count_increased")
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "primary_metric": primary_metric,
                    "metric": primary_metric,
                    "checkpoint": int(checkpoint),
                    "n_seed": int(len(deltas)),
                    "seed_list": "|".join(f"{int(seed):02d}" for seed in seed_values),
                    "primary_delta_mean": float(np.mean(deltas)) if deltas.size else float("nan"),
                    "primary_delta_sd": delta_sd,
                    "primary_delta_se": float(delta_sd / math.sqrt(deltas.size)) if deltas.size > 1 and pd.notna(delta_sd) else float("nan"),
                    "primary_improvement_mean": _mean_or_nan(sub["primary_improvement"]) if not sub.empty else float("nan"),
                    "bootstrap_ci_low": bootstrap_low,
                    "bootstrap_ci_high": bootstrap_high,
                    "direction_improved_seed_count": int(improved_mask.sum()),
                    "direction_improved_rate": float(improved_mask.mean()) if len(improved_mask) else float("nan"),
                    "informative_primary_seed_count": int(informative_mask.sum()),
                    "paired_t_stat": t_stat,
                    "paired_t_p": t_p,
                    "wilcoxon_p": wilcoxon_p,
                    "pocock_boundary": float(boundary),
                    "vehicle_delay_cost": _mean_or_nan(sub["vehicle_delay_cost"]) if not sub.empty else float("nan"),
                    "pedestrian_wait_improvement": _mean_or_nan(sub["pedestrian_wait_improvement"]) if not sub.empty else float("nan"),
                    "safety_improvement": _mean_or_nan(sub["safety_improvement"]) if not sub.empty else float("nan"),
                    "smart_extension_count_total": smart_extension_count_total,
                    "baseline_extension_count_total": baseline_extension_count_total,
                    "extension_seen_seed_count": extension_seen_seed_count,
                    "no_extension_streak": int(no_extension_streak),
                    "extension_trigger_rate": float(extension_seen_seed_count / max(len(sub), 1)),
                    "baseline_row_count": baseline_rows,
                    "smart_row_count": smart_rows,
                    "complete_pair_count": complete_pairs,
                    "route_demand_manifest_missing": bool(sub["route_demand_manifest_missing"].fillna(False).astype(bool).any()),
                    "extension_policy_violation_count": extension_violation_count,
                    "traffic_bad_reason": ";".join(traffic_bad_reasons),
                    "missing_pair_count": int(sub[["primary_baseline", "primary_smart"]].isna().any(axis=1).sum()) if not sub.empty else 0,
                    "failed_cases_count": int(pd.to_numeric(sub["failed_cases_count"], errors="coerce").fillna(0).sum()) if not sub.empty else 0,
                    "test_reasons": ";".join(test_reasons),
                    "quality_reasons": ";".join(dict.fromkeys(quality_reasons)),
                    **sources,
                }
            )
            if checkpoint >= max_checkpoint:
                break

    result = pd.DataFrame(rows)
    if result.empty:
        return result

    result = _add_tradeoff_and_gate(
        result,
        traffic_cost_weight=traffic_cost_weight,
        traffic_bad_quantile=traffic_bad_quantile,
        traffic_cost_max=traffic_cost_max,
    )
    result = _add_cutoff_metadata(result)

    result["graduation_round"] = result["checkpoint"].apply(
        lambda value: f"n{int(value)}" if int(value) in GRADUATION_ROUNDS else ""
    )
    result["eligible_for_graduation"] = result.apply(_eligible_for_graduation, axis=1)
    result["graduation_p_boundary"] = result["checkpoint"].apply(lambda value: _graduation_boundary(int(value))[0])
    result["graduation_direction_threshold"] = result["checkpoint"].apply(lambda value: _graduation_boundary(int(value))[1])

    statuses: list[str] = []
    reasons: list[str] = []
    next_checkpoints: list[str] = []
    for _, row in result.iterrows():
        checkpoint = int(row["checkpoint"])
        if checkpoint <= 1:
            quality_reason = _quality_recheck_reason(row)
            status, reason = ("RECHECK", quality_reason) if quality_reason else ("KEEP", "n1_quality_smoke_only")
        else:
            quality_reason = _quality_recheck_reason(row)
            hard_cut_reason = _hard_cut_reason(row)
            graduation_reason = _graduation_reason(row)
            if quality_reason:
                status, reason = "RECHECK", quality_reason
            elif hard_cut_reason:
                status, reason = "CUT", hard_cut_reason
            elif graduation_reason:
                status, reason = "PASS", graduation_reason
            elif bool(row.get("cut_applied", False)):
                status, reason = "CUT", f"tradeoff_bottom_cutoff_{row.get('cut_round', '')}"
            else:
                status, reason = "KEEP", "continue_to_next_checkpoint"
        statuses.append(status)
        reasons.append(reason)
        next_checkpoints.append("" if status in {"PASS", "CUT", "RECHECK"} else _next_active_checkpoint(checkpoints, int(row["checkpoint"])))
    result["pass_cut_keep_recheck"] = statuses
    result["next_checkpoint"] = next_checkpoints
    result["stop_reason"] = reasons
    result["qualified_pool_member"] = result["pass_cut_keep_recheck"].isin(["PASS", "KEEP"])

    result["bh_fdr_q"] = np.nan
    result["holm_p"] = np.nan
    for (_, checkpoint), idx in result.groupby(["metric", "checkpoint"]).groups.items():
        pvalues = result.loc[idx, "paired_t_p"]
        result.loc[idx, "bh_fdr_q"] = _bh_qvalues(pvalues)
        result.loc[idx, "holm_p"] = _holm_pvalues(pvalues)

    ordered = [
        "candidate_id",
        "checkpoint",
        "n_seed",
        "seed_list",
        "cut_round",
        "cut_rank",
        "cut_percentile",
        "cut_applied",
        "graduation_round",
        "eligible_for_graduation",
        "primary_metric",
        "primary_delta_mean",
        "primary_improvement_mean",
        "paired_t_p",
        "wilcoxon_p",
        "bootstrap_ci_low",
        "bootstrap_ci_high",
        "direction_improved_rate",
        "smart_extension_count_total",
        "baseline_extension_count_total",
        "extension_seen_seed_count",
        "extension_trigger_rate",
        "baseline_row_count",
        "smart_row_count",
        "complete_pair_count",
        "traffic_gate_pass",
        "traffic_bad_reason",
        "route_demand_manifest_missing",
        "tradeoff_score",
        "pass_cut_keep_recheck",
        "next_checkpoint",
        "stop_reason",
        "quality_reasons",
        "primary_delta_sd",
        "primary_delta_se",
        "paired_t_stat",
        "direction_improved_seed_count",
        "informative_primary_seed_count",
        "no_extension_streak",
        "extension_policy_violation_count",
        "cut_threshold_percentile",
        "cut_margin",
        "graduation_p_boundary",
        "graduation_direction_threshold",
        "qualified_pool_member",
        "vehicle_delay_cost",
        "pedestrian_wait_improvement",
        "safety_improvement",
        "missing_pair_count",
        "failed_cases_count",
    ]
    rest = [column for column in result.columns if column not in ordered]
    return result[ordered + rest].sort_values(["checkpoint", "pass_cut_keep_recheck", "tradeoff_score"], ascending=[True, True, False], na_position="last")


def write_selected_candidates(
    *,
    summary: pd.DataFrame,
    candidate_csv: str,
    selected_output: str,
    top_k: int,
    statuses: set[str],
) -> None:
    candidates = pd.read_csv(Path(candidate_csv).expanduser(), encoding="utf-8-sig")
    if "crosswalk_id" not in candidates.columns:
        raise ValueError(f"candidate csv missing crosswalk_id: {candidate_csv}")
    latest = (
        summary.sort_values(["candidate_id", "checkpoint"])
        .groupby("candidate_id", as_index=False)
        .tail(1)
        .copy()
    )
    eligible = latest[latest["pass_cut_keep_recheck"].astype(str).str.upper().isin(statuses)].copy()
    if "traffic_gate_pass" in eligible.columns:
        eligible = eligible[eligible["traffic_gate_pass"].astype(bool)]
    eligible = eligible.sort_values(
        ["tradeoff_score", "vehicle_delay_cost", "primary_improvement_mean"],
        ascending=[False, True, False],
        na_position="last",
    ).head(int(top_k))
    selected_ids = set(eligible["candidate_id"].astype(str).tolist())
    selected = candidates[candidates["crosswalk_id"].astype(str).isin(selected_ids)].copy()
    selected["_selection_rank"] = selected["crosswalk_id"].astype(str).map(
        {candidate_id: rank + 1 for rank, candidate_id in enumerate(eligible["candidate_id"].astype(str).tolist())}
    )
    selected = selected.sort_values("_selection_rank").drop(columns=["_selection_rank"])
    write_csv_utf8_sig(selected, Path(selected_output).expanduser(), index=False)
    print(f"wrote {selected_output} selected_candidates={len(selected)}")


def write_status_candidates(
    *,
    summary: pd.DataFrame,
    candidate_csv: str,
    output: str,
    statuses: set[str],
) -> None:
    candidates = pd.read_csv(Path(candidate_csv).expanduser(), encoding="utf-8-sig")
    if "crosswalk_id" not in candidates.columns:
        raise ValueError(f"candidate csv missing crosswalk_id: {candidate_csv}")
    latest = (
        summary.sort_values(["candidate_id", "checkpoint"])
        .groupby("candidate_id", as_index=False)
        .tail(1)
        .copy()
    )
    eligible = latest[latest["pass_cut_keep_recheck"].astype(str).str.upper().isin(statuses)].copy()
    ordered_ids = eligible.sort_values(
        ["pass_cut_keep_recheck", "tradeoff_score", "vehicle_delay_cost"],
        ascending=[True, False, True],
        na_position="last",
    )["candidate_id"].astype(str).tolist()
    selected = candidates[candidates["crosswalk_id"].astype(str).isin(set(ordered_ids))].copy()
    selected["_selection_rank"] = selected["crosswalk_id"].astype(str).map(
        {candidate_id: rank + 1 for rank, candidate_id in enumerate(ordered_ids)}
    )
    selected = selected.sort_values("_selection_rank").drop(columns=["_selection_rank"])
    write_csv_utf8_sig(selected, Path(output).expanduser(), index=False)
    print(f"wrote {output} statuses={','.join(sorted(statuses))} candidates={len(selected)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Paired sequential stopping analysis for sampled10 group outputs."
    )
    parser.add_argument("--input-dir", action="append", default=[], help="Seed output directory or CSV path.")
    parser.add_argument("--run-glob", action="append", default=[], help="Glob for seed output directories or CSV paths.")
    parser.add_argument("--candidate-csv", default=None, help="Candidate CSV with crosswalk_id column.")
    parser.add_argument("--output", required=True, help="Summary CSV output path.")
    parser.add_argument(
        "--primary-metric",
        default=PRIMARY_METRIC,
        choices=[
            "pedestrian_clearance_failure_delta",
            "unfinished_crossing_delta",
            "low_pet_event_count_delta",
            "very_risky_crossing_count_delta",
            "risky_crossing_count_delta",
            "pedestrian_wait_delta",
        ],
    )
    parser.add_argument("--pocock-boundary", type=float, default=0.0125)
    parser.add_argument("--checkpoints", default=",".join(str(value) for value in DEFAULT_CHECKPOINTS))
    parser.add_argument("--traffic-cost-weight", type=float, default=2.0)
    parser.add_argument("--traffic-bad-quantile", type=float, default=0.75)
    parser.add_argument("--traffic-cost-max", type=float, default=None)
    parser.add_argument("--cut-p-value", type=float, default=0.50, help="Deprecated; cut-off uses fixed n5/n10 rank rounds.")
    parser.add_argument("--cut-score-quantile", type=float, default=0.40, help="Deprecated; cut-off uses fixed n5/n10 rank rounds.")
    parser.add_argument("--selected-output", default=None, help="Optional selected candidate CSV output.")
    parser.add_argument("--selected-top-k", type=int, default=3)
    parser.add_argument("--selected-statuses", default="KEEP", help="Comma-separated statuses eligible for selected output.")
    parser.add_argument("--keep-output", default=None, help="Optional candidate CSV for KEEP rows only.")
    parser.add_argument("--pass-output", default=None, help="Optional candidate CSV for PASS/qualified rows only.")
    parser.add_argument("--cut-output", default=None, help="Optional candidate CSV for CUT rows only.")
    parser.add_argument("--recheck-output", default=None, help="Optional candidate CSV for RECHECK rows only.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    checkpoints = _parse_checkpoints(args.checkpoints)
    input_paths = _expand_inputs(args.input_dir, args.run_glob)
    if not input_paths:
        raise FileNotFoundError("no simulation_result.csv or simulation_results_seed.csv inputs found")

    seed_df = _load_seed_results(input_paths)
    candidate_ids = _load_candidate_ids(args.candidate_csv, seed_df)
    if not candidate_ids:
        raise ValueError("no candidate ids found")
    summary = analyze(
        seed_df=seed_df,
        candidate_ids=candidate_ids,
        checkpoints=checkpoints,
        boundary=float(args.pocock_boundary),
        primary_metric=str(args.primary_metric),
        traffic_cost_weight=float(args.traffic_cost_weight),
        traffic_bad_quantile=float(args.traffic_bad_quantile),
        traffic_cost_max=args.traffic_cost_max,
    )
    output_path = Path(args.output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv_utf8_sig(summary, output_path, index=False)
    print(f"wrote {output_path} rows={len(summary)}")
    if args.selected_output:
        if not args.candidate_csv:
            raise ValueError("--selected-output requires --candidate-csv")
        write_selected_candidates(
            summary=summary,
            candidate_csv=args.candidate_csv,
            selected_output=args.selected_output,
            top_k=int(args.selected_top_k),
            statuses=_parse_statuses(args.selected_statuses),
        )
    status_outputs = (
        (args.keep_output, {"KEEP"}),
        (args.pass_output, {"PASS"}),
        (args.cut_output, {"CUT"}),
        (args.recheck_output, {"RECHECK"}),
    )
    for output, statuses in status_outputs:
        if not output:
            continue
        if not args.candidate_csv:
            raise ValueError("status candidate outputs require --candidate-csv")
        write_status_candidates(
            summary=summary,
            candidate_csv=args.candidate_csv,
            output=output,
            statuses=statuses,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
