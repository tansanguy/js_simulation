#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

try:  # optional
    from scipy import stats  # type: ignore
except Exception:  # pragma: no cover
    stats = None


PAIR_KEYS = ["seed", "vehicle_hourly_volume", "crosswalk_id", "candidate_scope", "output_layout"]
METRICS = [
    "extension_count",
    "veh_waiting_time_mean",
    "veh_time_loss_mean",
    "impact_100m_waiting_time_mean",
    "impact_100m_time_loss_mean",
    "impact_300m_waiting_time_mean",
    "impact_300m_time_loss_mean",
    "impact_500m_waiting_time_mean",
    "impact_500m_time_loss_mean",
    "impact_1000m_waiting_time_mean",
    "impact_1000m_time_loss_mean",
    "impact_global_waiting_time_mean",
    "impact_global_time_loss_mean",
]


def _safe_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _load_results(manifest: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    rows: list[pd.DataFrame] = []
    missing: list[str] = []
    for item in manifest.itertuples(index=False):
        out_dir = Path(getattr(item, "output_dir"))
        scenario = str(getattr(item, "scenario"))
        result_path = out_dir / f"phase6_vehicle_flow_{scenario}_results.csv"
        if not result_path.exists():
            missing.append(str(result_path))
            continue
        df = pd.read_csv(result_path)
        df["seed"] = int(getattr(item, "seed"))
        df["vehicle_hourly_volume"] = float(getattr(item, "vehicle_hourly_volume"))
        df["sim_duration"] = int(getattr(item, "sim_duration"))
        df["warmup"] = int(getattr(item, "warmup"))
        df["extension_sec"] = float(getattr(item, "extension_sec"))
        df["candidate_scope"] = str(getattr(item, "candidate_scope", "all"))
        df["output_layout"] = str(getattr(item, "output_layout", "grouped"))
        df["run_unit"] = str(getattr(item, "run_unit", "batch"))
        df["manifest_crosswalk_id"] = str(getattr(item, "crosswalk_id", "ALL"))
        df["scenario"] = scenario
        df["output_dir"] = str(out_dir)
        rows.append(df)
    if rows:
        return pd.concat(rows, ignore_index=True), missing
    return pd.DataFrame(), missing


def _paired_summary(pair_df: pd.DataFrame) -> pd.DataFrame:
    summary_rows: list[dict[str, object]] = []
    for group_keys, group in pair_df.groupby(["vehicle_hourly_volume", "output_layout", "candidate_scope", "crosswalk_id"], dropna=False):
        volume, output_layout, candidate_scope, crosswalk_id = group_keys
        for metric in METRICS:
            diff_col = f"diff_{metric}"
            baseline_col = f"baseline_{metric}"
            smart_col = f"smart_{metric}"
            diff = _safe_numeric(group[diff_col]).dropna()
            base = _safe_numeric(group[baseline_col]).dropna()
            smart = _safe_numeric(group[smart_col]).dropna()
            n = int(len(diff))
            mean_diff = float(diff.mean()) if n else float("nan")
            std_diff = float(diff.std(ddof=1)) if n > 1 else float("nan")
            se_diff = float(std_diff / np.sqrt(n)) if n > 1 and np.isfinite(std_diff) else float("nan")

            t_stat = float("nan")
            p_value = float("nan")
            ci_low = float("nan")
            ci_high = float("nan")
            if n > 1 and np.isfinite(se_diff):
                if stats is not None:
                    try:
                        t_res = stats.ttest_rel(_safe_numeric(group[smart_col]), _safe_numeric(group[baseline_col]), nan_policy="omit")
                        t_stat = float(t_res.statistic)
                        p_value = float(t_res.pvalue)
                        crit = float(stats.t.ppf(0.975, df=n - 1))
                    except Exception:
                        crit = 1.96
                else:
                    crit = 1.96
                ci_low = float(mean_diff - crit * se_diff)
                ci_high = float(mean_diff + crit * se_diff)

            summary_rows.append(
                {
                    "vehicle_hourly_volume": float(volume),
                    "output_layout": output_layout,
                    "candidate_scope": candidate_scope,
                    "crosswalk_id": crosswalk_id,
                    "metric": metric,
                    "n_pairs": n,
                    "baseline_mean": float(base.mean()) if len(base) else float("nan"),
                    "smart_mean": float(smart.mean()) if len(smart) else float("nan"),
                    "diff_mean": mean_diff,
                    "diff_std": std_diff,
                    "diff_se": se_diff,
                    "ci95_low": ci_low,
                    "ci95_high": ci_high,
                    "t_stat": t_stat,
                    "p_value": p_value,
                }
            )
    return pd.DataFrame(summary_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze paired Phase 6 vehicle experiment outputs.")
    parser.add_argument("--manifest-csv", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    manifest = pd.read_csv(args.manifest_csv)
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    combined, missing = _load_results(manifest)
    if combined.empty:
        raise SystemExit("no result files found from manifest")

    baseline = combined[combined["scenario"].astype(str) == "baseline"].copy()
    smart = combined[combined["scenario"].astype(str) == "smart"].copy()

    merge_cols = [col for col in PAIR_KEYS if col in baseline.columns and col in smart.columns]
    merged = baseline.merge(smart, on=merge_cols, suffixes=("_baseline", "_smart"))

    for metric in METRICS:
        for side in ("baseline", "smart"):
            col = f"{metric}_{side}"
            if col in merged.columns:
                merged[col] = _safe_numeric(merged[col])
        if f"{metric}_baseline" in merged.columns and f"{metric}_smart" in merged.columns:
            merged[f"diff_{metric}"] = merged[f"{metric}_smart"] - merged[f"{metric}_baseline"]

    paired_path = output_dir / "phase6_vehicle_paired_differences.csv"
    merged.to_csv(paired_path, index=False)

    summary = _paired_summary(merged)
    summary_path = output_dir / "phase6_vehicle_paired_summary.csv"
    summary.to_csv(summary_path, index=False)

    md_path = output_dir / "phase6_vehicle_analysis_summary.md"
    lines = [
        "# Phase 6 Vehicle Experiment Analysis",
        "",
        f"- manifest rows: {len(manifest)}",
        f"- paired rows: {len(merged)}",
        f"- missing result files: {len(missing)}",
        f"- output_layouts: {sorted(set(manifest.get('output_layout', pd.Series(dtype=str)).astype(str).tolist())) if 'output_layout' in manifest.columns else []}",
        "",
        "## Notes",
        "- Paired difference is `smart - baseline`.",
        "- 95% CI is based on paired differences.",
        "- p-value is optional; use it as a secondary check only.",
        "- 30 repeats help stabilize the mean, but do not guarantee significance.",
    ]
    if missing:
        lines.extend(["", "## Missing Files"] + [f"- {path}" for path in missing[:20]])
        if len(missing) > 20:
            lines.append(f"- ... and {len(missing) - 20} more")
    if not summary.empty:
        lines.extend(["", "## Summary Preview"])
        try:
            preview = summary.head(40).to_markdown(index=False)
        except Exception:
            preview = summary.head(40).to_csv(index=False)
        lines.append(preview)
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"paired_differences_csv: {paired_path}")
    print(f"paired_summary_csv: {summary_path}")
    print(f"analysis_md: {md_path}")


if __name__ == "__main__":
    main()
