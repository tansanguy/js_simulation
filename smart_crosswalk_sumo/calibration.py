from __future__ import annotations

import argparse
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.errors import EmptyDataError


def geh(sim: float, obs: float) -> float:
    if sim + obs <= 0:
        return 0.0
    return math.sqrt(2.0 * (sim - obs) ** 2 / (sim + obs))


def mape(sim: float, obs: float) -> float:
    if obs == 0:
        return 0.0
    return abs(sim - obs) / abs(obs) * 100.0


def rmse(values: list[float]) -> float:
    if not values:
        return 0.0
    arr = np.asarray(values, dtype=float)
    return float(np.sqrt(np.mean(arr ** 2)))


def load_observed_counts(path: str | Path, representative_day_id: str | None) -> pd.DataFrame:
    df = pd.read_csv(path)
    if representative_day_id:
        if "representative_day_id" in df.columns:
            filtered = df[df["representative_day_id"].astype(str) == str(representative_day_id)]
        else:
            filtered = df[df["datetime"].astype(str).str.startswith(str(representative_day_id))]
        if not filtered.empty:
            df = filtered
    return df


def parse_edge_data(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["time", "edge_id", "queue", "mean_speed_mps"])
    root = ET.parse(path).getroot()
    rows = []
    for edge in root.findall("edge"):
        rows.append(
            {
                "time": float(edge.attrib.get("time", "0")),
                "edge_id": edge.attrib.get("id", ""),
                "queue": float(edge.attrib.get("queue", "0")),
                "mean_speed_mps": float(edge.attrib.get("meanSpeed", "0")),
            }
        )
    return pd.DataFrame(rows)


def calibrate(
    output_dir: str | Path,
    observed_counts_csv: str | Path | None,
    representative_day_id: str | None = None,
) -> tuple[pd.DataFrame, str]:
    output_dir = Path(output_dir)
    sim_seed_path = output_dir / "simulation_results_seed.csv"
    if not sim_seed_path.exists() or not observed_counts_csv:
        empty = pd.DataFrame()
        summary = "# Calibration Summary\n\n관측 교통량 또는 시뮬레이션 결과가 없어 calibration을 수행하지 않았습니다.\n"
        (output_dir / "calibration_report.csv").write_text("", encoding="utf-8")
        (output_dir / "calibration_summary.md").write_text(summary, encoding="utf-8")
        return empty, summary

    try:
        sim_seed = pd.read_csv(sim_seed_path)
    except EmptyDataError:
        empty = pd.DataFrame()
        summary = "# Calibration Summary\n\n시뮬레이션 결과가 비어 calibration을 수행하지 않았습니다.\n"
        (output_dir / "calibration_report.csv").write_text("", encoding="utf-8")
        (output_dir / "calibration_summary.md").write_text(summary, encoding="utf-8")
        return empty, summary
    obs = load_observed_counts(observed_counts_csv, representative_day_id)
    obs["volume"] = pd.to_numeric(obs.get("volume"), errors="coerce").fillna(0.0)
    if "speed_kph" in obs.columns:
        obs["speed_kph"] = pd.to_numeric(obs.get("speed_kph"), errors="coerce")

    baseline = sim_seed[sim_seed["scenario"] == "baseline"].copy()
    report_rows = []

    # Aggregate simulated throughput proxy at run-level.
    sim_vol_proxy = float(np.nanmean(baseline.get("surrounding_throughput_veh_per_hour", pd.Series([0.0]))))
    obs_volume_hour = float(obs["volume"].sum())

    volume_geh = geh(sim_vol_proxy, obs_volume_hour)
    volume_mape = mape(sim_vol_proxy, obs_volume_hour)

    speed_errors: list[float] = []
    speed_mapes: list[float] = []
    if "speed_kph" in obs.columns:
        edge_data_frames = []
        for edge_file in output_dir.glob("edge_data_baseline_seed*.xml"):
            df = parse_edge_data(edge_file)
            if not df.empty:
                edge_data_frames.append(df)
        if edge_data_frames:
            edge_data = pd.concat(edge_data_frames, ignore_index=True)
            speed_by_edge = edge_data.groupby("edge_id", as_index=False)["mean_speed_mps"].mean()
            speed_by_edge["sim_speed_kph"] = speed_by_edge["mean_speed_mps"] * 3.6
            merged = obs.merge(speed_by_edge[["edge_id", "sim_speed_kph"]], on="edge_id", how="left")
            for row in merged.itertuples(index=False):
                if pd.isna(getattr(row, "speed_kph", np.nan)) or pd.isna(getattr(row, "sim_speed_kph", np.nan)):
                    continue
                err = float(row.sim_speed_kph) - float(row.speed_kph)
                speed_errors.append(err)
                speed_mapes.append(mape(float(row.sim_speed_kph), float(row.speed_kph)))

    queue_error = np.nan
    if "queue_length" in obs.columns:
        obs_q = pd.to_numeric(obs["queue_length"], errors="coerce").dropna()
        sim_q = baseline.get("surrounding_queue_total_avg", pd.Series(dtype=float)).dropna()
        if not obs_q.empty and not sim_q.empty:
            queue_error = float(np.nanmean(sim_q) - np.nanmean(obs_q))

    bottleneck_onset_diff = np.nan
    bottleneck_duration_diff = np.nan

    report_rows.append(
        {
            "representative_day_id": representative_day_id if representative_day_id else "all",
            "volume_simulated": sim_vol_proxy,
            "volume_observed": obs_volume_hour,
            "volume_GEH": volume_geh,
            "volume_MAPE_pct": volume_mape,
            "speed_RMSE_kph": rmse(speed_errors),
            "speed_MAPE_pct": float(np.nanmean(speed_mapes)) if speed_mapes else np.nan,
            "queue_length_error": queue_error,
            "bottleneck_onset_time_diff_sec": bottleneck_onset_diff,
            "bottleneck_duration_diff_sec": bottleneck_duration_diff,
        }
    )

    report_df = pd.DataFrame(report_rows)
    report_df.to_csv(output_dir / "calibration_report.csv", index=False)

    summary = f"""# Calibration Summary

- representative_day_id: {representative_day_id if representative_day_id else 'all'}
- volume GEH: {volume_geh:.3f}
- volume MAPE: {volume_mape:.2f}%
- speed RMSE: {rmse(speed_errors):.3f} kph
- speed MAPE: {(float(np.nanmean(speed_mapes)) if speed_mapes else float('nan')):.2f}%
- queue length error: {queue_error if not np.isnan(queue_error) else 'N/A'}
- bottleneck onset/duration difference: 관측 queue time-series 미제공 시 N/A

FHWA microsimulation calibration 취지에 맞춰 대표일 기반 비교를 지원하며, synthetic single-day fitting보다 관측 대표일을 우선한다.
"""
    (output_dir / "calibration_summary.md").write_text(summary, encoding="utf-8")
    return report_df, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output_dir", default="outputs")
    parser.add_argument("--observed_counts", default=None)
    parser.add_argument("--representative_day_id", default=None)
    args = parser.parse_args()
    calibrate(args.output_dir, args.observed_counts, args.representative_day_id)


if __name__ == "__main__":
    main()
