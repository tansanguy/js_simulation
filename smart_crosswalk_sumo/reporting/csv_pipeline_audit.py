#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from smart_crosswalk_sumo.phase6_result_standardizer import build_cleanup_candidates


SAFETY_KEYWORDS = ["accident", "crash", "collision", "risk", "conflict", "near_miss", "ttc", "pet", "safety", "hazard", "proximity"]
RUNTIME_COLUMNS = {"run_start_time", "run_end_time", "elapsed_sec", "completed", "run_name", "output_dir"}
VEHICLE_COLUMNS = {"vehicle_", "veh_", "avg_vehicle_delay_sec", "network_arrived_vehicles", "total_vehicle_delay_sec", "vehicle_delay_mean", "veh_time_loss_mean"}
PEDESTRIAN_COLUMNS = {"ped_", "pedestrian_", "PET_", "crossing_", "incomplete_crossing_count", "elderly_incomplete_crossings"}
SAFETY_SUMMARY_COLUMNS = [
    "run_name",
    "scenario",
    "seed",
    "crosswalk_id",
    "ped_crossing_person_count",
    "ped_crossing_presence_steps",
    "extension_count",
    "ped_vehicle_conflict_count",
    "high_risk_event_count",
    "near_miss_count",
    "min_ttc",
    "mean_ttc",
    "min_pet",
    "mean_pet",
    "risk_event_per_pedestrian",
    "risk_event_per_crossing_presence_step",
    "elapsed_sec",
    "source_csv",
    "note",
]


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_csv_meta(path: Path) -> tuple[int, int, list[str], str]:
    last_error = ""
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            with path.open("r", encoding=encoding, newline="", errors="replace") as f:
                reader = csv.reader(f)
                header = next(reader, [])
                row_count = sum(1 for _ in reader)
            return row_count, len(header), list(header), ""
        except Exception as exc:  # pragma: no cover - defensive
            last_error = str(exc)
    return 0, 0, [], last_error or "unreadable_csv"


def _has_any(columns: list[str], needles: set[str] | list[str]) -> bool:
    lowered = [c.lower() for c in columns]
    for needle in needles:
        needle_l = needle.lower()
        if needle in columns:
            return True
        if any(needle_l in col for col in lowered):
            return True
    return False


def _detect_matching_columns(columns: list[str]) -> list[str]:
    lowered = [c.lower() for c in columns]
    matches: list[str] = []
    for needle in SAFETY_KEYWORDS:
        if any(needle in col for col in lowered):
            matches.append(needle)
    return matches


def _scan_csv_inventory(result_root: Path, exclude_roots: list[Path] | None = None) -> pd.DataFrame:
    exclude_roots = [p.resolve() for p in (exclude_roots or [])]
    rows: list[dict[str, Any]] = []
    for path in sorted(result_root.rglob("*.csv")):
        resolved = path.resolve()
        if any(str(resolved).startswith(str(root)) for root in exclude_roots):
            continue
        row_count, column_count, columns, note = _read_csv_meta(path)
        rows.append(
            {
                "file_path": str(path.relative_to(result_root)),
                "row_count": int(row_count),
                "column_count": int(column_count),
                "columns": json.dumps(columns, ensure_ascii=False),
                "has_scenario": bool("scenario" in columns),
                "has_seed": bool("seed" in columns),
                "has_crosswalk_id": bool("crosswalk_id" in columns or "target_crosswalk_id" in columns),
                "has_elapsed_sec": bool("elapsed_sec" in columns),
                "has_runtime_columns": bool(_has_any(columns, RUNTIME_COLUMNS)),
                "has_safety_columns": bool(_has_any(columns, SAFETY_KEYWORDS)),
                "has_vehicle_columns": bool(_has_any(columns, VEHICLE_COLUMNS)),
                "has_pedestrian_columns": bool(_has_any(columns, PEDESTRIAN_COLUMNS)),
                "note": note or ("empty_csv" if column_count == 0 else ""),
            }
        )
    return pd.DataFrame(rows)


def _scan_safety_presence(result_root: Path, inventory_df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, inv in inventory_df.iterrows():
        path = result_root / str(inv["file_path"])
        row_count, column_count, columns, note = _read_csv_meta(path)
        matches = _detect_matching_columns(columns)
        rows.append(
            {
                "file_path": str(inv["file_path"]),
                "row_count": int(row_count),
                "column_count": int(column_count),
                "has_accident": bool("accident" in [c.lower() for c in columns]),
                "has_crash": bool("crash" in [c.lower() for c in columns]),
                "has_collision": bool("collision" in [c.lower() for c in columns]),
                "has_risk": bool("risk" in [c.lower() for c in columns]),
                "has_conflict": bool("conflict" in [c.lower() for c in columns]),
                "has_near_miss": bool("near_miss" in [c.lower() for c in columns]),
                "has_ttc": bool("ttc" in [c.lower() for c in columns]),
                "has_pet": bool("pet" in [c.lower() for c in columns]),
                "has_safety": bool(_has_any(columns, SAFETY_KEYWORDS)),
                "has_hazard": bool("hazard" in [c.lower() for c in columns]),
                "has_proximity": bool("proximity" in [c.lower() for c in columns]),
                "matched_metric_columns": json.dumps(matches, ensure_ascii=False),
                "note": note or ("matches_safety_metrics" if matches else ""),
            }
        )
    return pd.DataFrame(rows)


def _read_frame(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except Exception:
            continue
    return pd.DataFrame()


def _pick_first(row: pd.Series, *names: str, default: Any = np.nan) -> Any:
    for name in names:
        if name in row.index and pd.notna(row[name]):
            return row[name]
    return default


def _safe_float(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _safe_bool(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, np.integer)):
        return bool(value)
    if isinstance(value, float) and np.isnan(value):
        return False
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n", ""}:
        return False
    return False


def _extract_safety_summary_rows(result_root: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for path in sorted(result_root.rglob("*.csv")):
        df = _read_frame(path)
        if df.empty:
            continue
        if not any(
            col in df.columns
            for col in [
                "high_risk_event_count",
                "risk_event_count",
                "safety_risk_score",
                "accident_expected_value",
                "PET_A_proxy_count",
                "PET_B_surrogate_count",
                "pedestrian_count_near_target",
                "crossing_attempt_count",
                "ttc_s",
                "pet_s",
            ]
        ):
            continue
        for _, row in df.iterrows():
            seed = _pick_first(row, "seed", default=np.nan)
            scenario = str(_pick_first(row, "scenario", default="")).strip()
            crosswalk_id = str(
                _pick_first(row, "crosswalk_id", "target_crosswalk_id", default="")
            ).strip()
            run_name = str(_pick_first(row, "run_name", default="")).strip()
            if not run_name:
                run_name = f"{scenario}_seed{seed}" if scenario or pd.notna(seed) else path.stem
            ped_count = _safe_float(_pick_first(row, "ped_crossing_person_count", "pedestrian_count_near_target", default=np.nan))
            presence_steps = _safe_float(_pick_first(row, "ped_crossing_presence_steps", default=np.nan))
            extension_count = _safe_float(_pick_first(row, "extension_count", "total_extension_count", default=np.nan))
            high_risk = _safe_float(_pick_first(row, "high_risk_event_count", "risk_event_count", default=np.nan))
            near_miss = _safe_float(_pick_first(row, "near_miss_count", default=np.nan))
            ttc_min = _safe_float(_pick_first(row, "min_ttc", "ttc_s", default=np.nan))
            ttc_mean = _safe_float(_pick_first(row, "mean_ttc", "ttc_s", default=np.nan))
            pet_min = _safe_float(_pick_first(row, "min_pet", "pet_s", default=np.nan))
            pet_mean = _safe_float(_pick_first(row, "mean_pet", "pet_s", default=np.nan))
            elapsed_sec = _safe_float(_pick_first(row, "elapsed_sec", default=np.nan))
            conflict_count = _safe_float(_pick_first(row, "ped_vehicle_conflict_count", "high_risk_event_count", "risk_event_count", default=np.nan))
            rows.append(
                {
                    "run_name": run_name,
                    "scenario": scenario,
                    "seed": seed,
                    "crosswalk_id": crosswalk_id,
                    "ped_crossing_person_count": ped_count,
                    "ped_crossing_presence_steps": presence_steps,
                    "extension_count": extension_count,
                    "ped_vehicle_conflict_count": conflict_count,
                    "high_risk_event_count": high_risk,
                    "near_miss_count": near_miss,
                    "min_ttc": ttc_min,
                    "mean_ttc": ttc_mean,
                    "min_pet": pet_min,
                    "mean_pet": pet_mean,
                    "risk_event_per_pedestrian": (high_risk / ped_count) if high_risk is not None and ped_count not in (None, 0) else np.nan,
                    "risk_event_per_crossing_presence_step": (high_risk / presence_steps) if high_risk is not None and presence_steps not in (None, 0) else np.nan,
                    "elapsed_sec": elapsed_sec,
                    "source_csv": str(path.relative_to(result_root)),
                    "note": "derived_from_existing_safety_csv" if any(col in df.columns for col in ["high_risk_event_count", "risk_event_count"]) else "partial_surrogate_fields_only",
                }
            )
    if not rows:
        return pd.DataFrame(columns=SAFETY_SUMMARY_COLUMNS)
    out = pd.DataFrame(rows)
    for col in SAFETY_SUMMARY_COLUMNS:
        if col not in out.columns:
            out[col] = np.nan
    return out[SAFETY_SUMMARY_COLUMNS].copy()


def _scenario_dir(path: Path) -> str:
    return path.name


def _find_pair_roots(seed30_root: Path) -> list[Path]:
    roots = []
    for path in sorted(seed30_root.rglob("baseline_seed*")):
        if path.is_dir() and (path / "phase6_smoke_baseline_results.csv").exists():
            roots.append(path.parent)
    return sorted({p.resolve() for p in roots})


def _extract_pair_rows(seed30_root: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    pair_rows: list[dict[str, Any]] = []
    metric_rows: list[dict[str, Any]] = []
    roots = _find_pair_roots(seed30_root)
    metrics = ["extension_count", "ped_crossing_person_count", "vehicle_delay_mean", "elapsed_sec"]
    for group_root in roots:
        baseline_dirs = sorted(group_root.glob("baseline_seed*"))
        for base_dir in baseline_dirs:
            seed_text = base_dir.name.split("baseline_seed", 1)[-1]
            smart_dir = group_root / f"smart_seed{seed_text}"
            base_path = base_dir / "phase6_smoke_baseline_results.csv"
            smart_path = smart_dir / "phase6_smoke_smart_results.csv"
            if not base_path.exists() or not smart_path.exists():
                continue
            base_df = _read_frame(base_path)
            smart_df = _read_frame(smart_path)
            if base_df.empty or smart_df.empty:
                continue
            base_row = base_df.iloc[0]
            smart_row = smart_df.iloc[0]
            crosswalk_id = str(_pick_first(base_row, "crosswalk_id", default="")).strip() or str(_pick_first(smart_row, "crosswalk_id", default="")).strip()
            base_vehicle = _pick_first(base_row, "veh_delay_mean", "veh_time_loss_mean", "avg_vehicle_delay_sec", default=np.nan)
            smart_vehicle = _pick_first(smart_row, "veh_delay_mean", "veh_time_loss_mean", "avg_vehicle_delay_sec", default=np.nan)
            baseline_completed = _safe_bool(_pick_first(base_row, "completed", default=False))
            smart_completed = _safe_bool(_pick_first(smart_row, "completed", default=False))
            pair_rows.append(
                {
                    "crosswalk_id": crosswalk_id,
                    "seed": int(float(seed_text)),
                    "baseline_output_dir": str(base_dir),
                    "smart_output_dir": str(smart_dir),
                    "baseline_completed": baseline_completed,
                    "smart_completed": smart_completed,
                    "baseline_extension_count": _pick_first(base_row, "extension_count", default=np.nan),
                    "smart_extension_count": _pick_first(smart_row, "extension_count", default=np.nan),
                    "baseline_elapsed_sec": _pick_first(base_row, "elapsed_sec", default=np.nan),
                    "smart_elapsed_sec": _pick_first(smart_row, "elapsed_sec", default=np.nan),
                    "baseline_ped_count": _pick_first(base_row, "ped_crossing_person_count", default=np.nan),
                    "smart_ped_count": _pick_first(smart_row, "ped_crossing_person_count", default=np.nan),
                    "baseline_vehicle_delay_mean": base_vehicle,
                    "smart_vehicle_delay_mean": smart_vehicle,
                "comparison_status": "ready" if baseline_completed and smart_completed else "incomplete_run",
                }
            )
        # metric summary from paired rows in this group
        group_df = pd.DataFrame([row for row in pair_rows if str(row.get("crosswalk_id", "")).strip() and Path(str(row["baseline_output_dir"])).parent == group_root])
        if group_df.empty:
            continue
        for crosswalk_id, sub in group_df.groupby("crosswalk_id", dropna=False):
            for metric in metrics:
                base_col = f"baseline_{metric}"
                smart_col = f"smart_{metric}"
                if base_col not in sub.columns or smart_col not in sub.columns:
                    continue
                base_vals = pd.to_numeric(sub[base_col], errors="coerce").dropna()
                smart_vals = pd.to_numeric(sub[smart_col], errors="coerce").dropna()
                n = min(len(base_vals), len(smart_vals))
                if n == 0:
                    continue
                base_mean = float(base_vals.mean())
                smart_mean = float(smart_vals.mean())
                base_std = float(base_vals.std(ddof=1)) if len(base_vals) > 1 else 0.0
                smart_std = float(smart_vals.std(ddof=1)) if len(smart_vals) > 1 else 0.0
                diff = smart_mean - base_mean
                pct = (diff / base_mean * 100.0) if pd.notna(base_mean) and abs(base_mean) > 1e-12 else np.nan
                interpretation = "lower is better" if metric in {"vehicle_delay_mean", "elapsed_sec"} else "higher is better or compare with context"
                metric_rows.append(
                    {
                        "crosswalk_id": crosswalk_id,
                        "metric": metric,
                        "baseline_mean": base_mean,
                        "smart_mean": smart_mean,
                        "difference": diff,
                        "percent_change": pct,
                        "baseline_std": base_std,
                        "smart_std": smart_std,
                        "n_seeds": int(n),
                        "interpretation": interpretation,
                    }
                )
    return pd.DataFrame(pair_rows), pd.DataFrame(metric_rows)


def _write_readme(path: Path, title: str, lines: list[str]) -> None:
    path.write_text("\n".join([f"# {title}", "", *lines, ""]) , encoding="utf-8")


def _write_csv_readmes(readme_dir: Path, tables: dict[str, pd.DataFrame], notes: dict[str, str]) -> None:
    for name, df in tables.items():
        lines = [
            f"- rows: `{len(df)}`",
            f"- columns: `{len(df.columns)}`",
            f"- note: {notes.get(name, '')}",
            "",
            "## Columns",
        ]
        if df.empty and not df.columns.tolist():
            lines.append("- file empty")
        else:
            for col in df.columns.tolist():
                lines.append(f"- `{col}`")
        _write_readme(readme_dir / f"{Path(name).stem}_README.md", f"{name} README", lines)


def _build_cleanup_inventory(result_root: Path) -> pd.DataFrame:
    raw = build_cleanup_candidates(result_root)
    rows: list[dict[str, Any]] = []
    for _, row in raw.iterrows():
        rows.append(
            {
                "file_path": row["file_path"],
                "file_size_mb": row.get("size_mb", np.nan),
                "reason": row.get("reason", ""),
                "safe_to_delete": bool(row.get("safe_to_delete", False)),
                "protected_reason": "" if bool(row.get("safe_to_delete", False)) else row.get("keep_reason_if_any", ""),
            }
        )

    seen = {r["file_path"] for r in rows}
    protected_markers = [
        "phase_next_top50_final_candidate_screening",
        "generated_crossing_patch",
        "command_to_run",
        "phase6_vehicle_experiment_300_30seed_human",
    ]
    for path in sorted(result_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(result_root).as_posix()
        if rel in seen:
            continue
        reason = ""
        protected_reason = ""
        safe = False
        if path.name in {"README.md", "run_manifest.csv", "run_completion_summary.csv"}:
            reason = "protected summary artifact"
            protected_reason = "root summary artifact"
        elif path.name.startswith("command_to_run") or path.suffix == ".sh":
            reason = "protected command template"
            protected_reason = "command_to_run script"
        elif path.suffix == ".net.xml":
            reason = "protected network file"
            protected_reason = "net.xml"
        elif any(marker in rel for marker in protected_markers):
            reason = "protected reference artifact"
            protected_reason = "reference result / screening artifact"
        if reason:
            rows.append(
                {
                    "file_path": rel,
                    "file_size_mb": round(path.stat().st_size / (1024 * 1024), 6),
                    "reason": reason,
                    "safe_to_delete": safe,
                    "protected_reason": protected_reason,
                }
            )
    return pd.DataFrame(rows, columns=["file_path", "file_size_mb", "reason", "safe_to_delete", "protected_reason"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit CSV pipeline outputs and assemble Phase 6 result inventories.")
    parser.add_argument("--result-root", default="result")
    parser.add_argument("--seed30-root", default="result/phase_next_top50_tiered_recovery_audit/seed30")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    result_root = Path(args.result_root).resolve()
    seed30_root = Path(args.seed30_root).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else result_root / f"phase_next_csv_pipeline_audit_{_now_stamp()}"
    csv_dir = output_dir / "csv"
    readme_dir = output_dir / "readme"
    figures_dir = output_dir / "figures"
    for path in [output_dir, csv_dir, readme_dir, figures_dir]:
        _ensure_dir(path)

    inventory_df = _scan_csv_inventory(result_root, exclude_roots=[output_dir])
    safety_presence_df = _scan_safety_presence(result_root, inventory_df)
    safety_summary_df = _extract_safety_summary_rows(result_root)
    pair_df, metric_df = _extract_pair_rows(seed30_root)
    cleanup_df = _build_cleanup_inventory(result_root)

    pair_df.to_csv(csv_dir / "seed30_baseline_smart_comparison.csv", index=False)
    metric_df.to_csv(csv_dir / "seed30_metric_summary.csv", index=False)
    safety_summary_df.to_csv(csv_dir / "safety_surrogate_summary.csv", index=False)
    inventory_df.to_csv(csv_dir / "csv_output_inventory.csv", index=False)
    safety_presence_df.to_csv(csv_dir / "safety_metric_presence_check.csv", index=False)
    cleanup_df.to_csv(csv_dir / "cleanup_candidate_inventory.csv", index=False)

    run_manifest_df = pair_df[
        [
            "crosswalk_id",
            "seed",
            "baseline_output_dir",
            "smart_output_dir",
            "baseline_completed",
            "smart_completed",
            "baseline_extension_count",
            "smart_extension_count",
            "baseline_elapsed_sec",
            "smart_elapsed_sec",
            "baseline_ped_count",
            "smart_ped_count",
            "baseline_vehicle_delay_mean",
            "smart_vehicle_delay_mean",
            "comparison_status",
        ]
    ].copy() if not pair_df.empty else pd.DataFrame(columns=[
        "crosswalk_id",
        "seed",
        "baseline_output_dir",
        "smart_output_dir",
        "baseline_completed",
        "smart_completed",
        "baseline_extension_count",
        "smart_extension_count",
        "baseline_elapsed_sec",
        "smart_elapsed_sec",
        "baseline_ped_count",
        "smart_ped_count",
        "baseline_vehicle_delay_mean",
        "smart_vehicle_delay_mean",
        "comparison_status",
    ])
    run_manifest_df.to_csv(output_dir / "run_manifest.csv", index=False)

    summary_df = pd.DataFrame(
        [
            {
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "result_root": str(result_root),
                "seed30_root": str(seed30_root),
                "csv_total": int(len(inventory_df)),
                "missing_meta_csv_count": int((~inventory_df["has_runtime_columns"]).sum()) if not inventory_df.empty else 0,
                "missing_runtime_csv_count": int((~inventory_df["has_elapsed_sec"]).sum()) if not inventory_df.empty else 0,
                "safety_csv_count": int(inventory_df["has_safety_columns"].sum()) if not inventory_df.empty else 0,
                "paired_run_count": int(len(pair_df)),
                "comparison_ready_count": int((pair_df["comparison_status"] == "ready").sum()) if not pair_df.empty and "comparison_status" in pair_df.columns else 0,
                "output_dir": str(output_dir),
            }
        ]
    )
    summary_df.to_csv(output_dir / "run_completion_summary.csv", index=False)

    tables = {
        "csv_output_inventory.csv": inventory_df,
        "safety_metric_presence_check.csv": safety_presence_df,
        "safety_surrogate_summary.csv": safety_summary_df,
        "seed30_baseline_smart_comparison.csv": pair_df,
        "seed30_metric_summary.csv": metric_df,
        "cleanup_candidate_inventory.csv": cleanup_df,
    }
    notes = {
        "csv_output_inventory.csv": "full inventory of CSV files under result root",
        "safety_metric_presence_check.csv": "flag safety surrogate metric families by file",
        "safety_surrogate_summary.csv": "rows built only from existing surrogate/safety CSVs",
        "seed30_baseline_smart_comparison.csv": "paired baseline vs smart seed30 comparison",
        "seed30_metric_summary.csv": "per-crosswalk metric summary across paired seeds",
        "cleanup_candidate_inventory.csv": "dry-run cleanup inventory; safe_to_delete is conservative",
    }
    _write_csv_readmes(readme_dir, tables, notes)

    root_readme_lines = [
        f"- result_root: `{result_root}`",
        f"- seed30_root: `{seed30_root}`",
        f"- csv_total: `{len(inventory_df)}`",
        f"- paired_run_count: `{len(pair_df)}`",
        f"- safety_csv_count: `{int(inventory_df['has_safety_columns'].sum()) if not inventory_df.empty else 0}`",
        f"- missing_runtime_csv_count: `{int((~inventory_df['has_runtime_columns']).sum()) if not inventory_df.empty else 0}`",
        "",
        "## Output Files",
        "",
        "- `run_manifest.csv`",
        "- `run_completion_summary.csv`",
        "- `csv/csv_output_inventory.csv`",
        "- `csv/safety_metric_presence_check.csv`",
        "- `csv/safety_surrogate_summary.csv`",
        "- `csv/seed30_baseline_smart_comparison.csv`",
        "- `csv/seed30_metric_summary.csv`",
        "- `csv/cleanup_candidate_inventory.csv`",
        "",
        "## Next 30 Seed Command",
        "",
        "```bash",
        "bash command_to_run_next_30seed_batch.sh",
        "```",
    ]
    _write_readme(output_dir / "README.md", "Phase 6 CSV Pipeline Audit", root_readme_lines)

    command_script = output_dir / "command_to_run_next_30seed_batch.sh"
    command_script.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                "",
                "# Template only. Update candidate/net paths for actual 30-seed execution.",
                "RESULT_ROOT=\"result/phase6_transition_after_recovery_20260514_220549/phase6_vehicle_experiment_300_30seed\"",
                "for seed in $(seq 1 30); do",
                "  echo \"seed=${seed} baseline/smart batch placeholder\"",
                "  echo \"python3 -m smart_crosswalk_sumo.run_phase6_vehicle_flow ...\"",
                "done",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print(json.dumps({"output_dir": str(output_dir), "csv_total": int(len(inventory_df)), "paired_run_count": int(len(pair_df))}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
