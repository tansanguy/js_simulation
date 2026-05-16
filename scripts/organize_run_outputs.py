#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


SUMMARY_CATEGORIES = ("traffic", "safety", "other", "log_candidate")
LOG_BUCKETS = ("raw", "debug", "audit", "sumo", "none")

TRAFFIC_PATTERNS = (
    "vehicle_flow_policy_validation",
    "vehicle_global_coverage_validation",
    "vehicle_edge_coverage_summary",
    "vehicle_route_generation_audit",
    "road_group_allocation_summary",
    "demand_source_audit",
    "route_generation_audit",
)

SAFETY_PATTERNS = (
    "pedestrian_flow_policy_validation",
    "pedestrian_connectivity_audit",
    "pedestrian_route_connectivity_audit",
    "invalid_pedestrian_candidates",
    "invalid_pedestrian_routes",
    "skipped_pedestrian_routes",
)

OTHER_PATTERNS = (
    "demand_params",
    "candidates",
    "preprocessed_crosswalks",
    "lane_count_updates",
    "model_assumptions_used",
    "network_mode_warnings",
    "pipeline_freshness",
    "vehicle_demand_pipeline_audit",
    "calibration_report",
    "calibration_summary",
)

AUDIT_PATTERNS = (
    "audit",
    "calibration",
    "pipeline_freshness",
    "demand_params",
    "road_group_allocation",
    "vehicle_edge_coverage",
    "vehicle_route_generation",
    "demand_source",
    "route_generation",
    "model_assumptions",
    "network_mode_warnings",
    "lane_count_updates",
    "preprocessed",
    "candidates",
)

DEBUG_PATTERNS = (
    "invalid",
    "skipped",
    "connectivity",
    "warnings",
)


@dataclass
class ArtifactRecord:
    source_path: Path
    relative_path: str
    summary_category: str
    log_bucket: str
    action: str
    target_path: str
    reason: str
    artifact_type: str
    include_in_summary: bool
    row_count: int | str = ""
    column_count: int | str = ""
    key_metric: str = ""
    key_value: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": str(self.source_path),
            "relative_path": self.relative_path,
            "summary_category": self.summary_category,
            "log_bucket": self.log_bucket,
            "action": self.action,
            "target_path": self.target_path,
            "reason": self.reason,
            "artifact_type": self.artifact_type,
            "include_in_summary": int(bool(self.include_in_summary)),
            "row_count": self.row_count,
            "column_count": self.column_count,
            "key_metric": self.key_metric,
            "key_value": self.key_value,
            "note": self.note,
        }


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except Exception:
        try:
            return pd.read_csv(path)
        except Exception:
            return pd.DataFrame()


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except Exception:
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""


def _classify_summary_category(path: Path) -> tuple[str, str]:
    name = path.name.lower()
    if any(token in name for token in SAFETY_PATTERNS):
        return "safety", "safety metric or safety debug"
    if any(token in name for token in TRAFFIC_PATTERNS):
        return "traffic", "traffic metric or vehicle demand validation"
    if any(token in name for token in OTHER_PATTERNS):
        return "other", "policy, input, metadata, or audit summary"
    if path.suffix.lower() in {".xml", ".sumocfg", ".net"} or name.endswith(".net.xml") or name.endswith(".rou.xml") or name.endswith(".trips.xml"):
        return "log_candidate", "raw SUMO artifact"
    if path.suffix.lower() in {".log"}:
        return "log_candidate", "raw runtime log"
    if path.suffix.lower() in {".json", ".md", ".csv"}:
        return "other", "general summary or metadata"
    return "other", "general summary or metadata"


def _classify_log_bucket(path: Path) -> str:
    name = path.name.lower()
    if path.suffix.lower() in {".log", ".osm"} or name == "run_metadata.json":
        return "raw"
    if name.endswith(".net.xml") or name.endswith(".rou.xml") or name.endswith(".trips.xml") or name.endswith(".sumocfg") or path.suffix.lower() == ".xml":
        return "sumo"
    if any(token in name for token in DEBUG_PATTERNS):
        return "debug"
    if any(token in name for token in AUDIT_PATTERNS):
        return "audit"
    if path.suffix.lower() in {".json", ".md", ".csv"}:
        return "audit"
    return "audit"


def _summary_for_csv(path: Path, category: str) -> dict[str, Any]:
    df = _safe_read_csv(path)
    if df.empty:
        return {
            "row_count": 0,
            "column_count": 0,
            "key_metric": "rows",
            "key_value": "0",
            "note": "empty_or_unreadable",
        }

    cols = list(df.columns)
    row_count = int(len(df))
    column_count = int(len(cols))
    name = path.name.lower()
    note_parts: list[str] = []
    key_metric = "row_count"
    key_value = str(row_count)

    if name == "demand_params.csv":
        scenario_name = str(df["scenario_name"].iloc[0]) if "scenario_name" in df.columns and not df.empty else ""
        generated_vehicle_count = int(pd.to_numeric(df.get("generated_vehicle_count", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if "generated_vehicle_count" in df.columns else 0
        generated_pedestrian_count = int(pd.to_numeric(df.get("generated_pedestrian_count", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if "generated_pedestrian_count" in df.columns else 0
        total_vehicle_flow_vph = float(pd.to_numeric(df.get("total_vehicle_flow_vph", pd.Series(dtype=float)), errors="coerce").fillna(0).max()) if "total_vehicle_flow_vph" in df.columns else 0.0
        total_vehicle_count_600s = int(pd.to_numeric(df.get("total_vehicle_count_600s", pd.Series(dtype=float)), errors="coerce").fillna(0).max()) if "total_vehicle_count_600s" in df.columns else 0
        vehicle_type = str(df["vehicle_type"].iloc[0]) if "vehicle_type" in df.columns and not df.empty else ""
        passenger_ratio = float(pd.to_numeric(df.get("passenger_ratio", pd.Series(dtype=float)), errors="coerce").fillna(0).max()) if "passenger_ratio" in df.columns else 0.0
        road_group_allocation_sum = int(pd.to_numeric(df.get("road_allocated_count_600s", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if "road_allocated_count_600s" in df.columns else 0
        key_metric = "scenario_name"
        key_value = scenario_name
        note_parts.extend(
            [
                f"generated_vehicle_count={generated_vehicle_count}",
                f"generated_pedestrian_count={generated_pedestrian_count}",
                f"total_vehicle_flow_vph={total_vehicle_flow_vph}",
                f"total_vehicle_count_600s={total_vehicle_count_600s}",
                f"vehicle_type={vehicle_type}",
                f"passenger_ratio={passenger_ratio}",
                f"road_group_allocation_sum={road_group_allocation_sum}",
            ]
        )
    elif name in {"vehicle_route_generation_audit.csv", "vehicle_edge_coverage_summary.csv", "road_group_allocation_summary.csv"}:
        scenario_name = str(df["scenario_name"].iloc[0]) if "scenario_name" in df.columns and not df.empty else ""
        generated_vehicle_count = int(pd.to_numeric(df.get("generated_vehicle_count", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if "generated_vehicle_count" in df.columns else 0
        road_group_count = int(df["road_group"].astype(str).nunique()) if "road_group" in df.columns else 0
        road_allocated_sum = int(pd.to_numeric(df.get("road_allocated_count_600s", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if "road_allocated_count_600s" in df.columns else 0
        unique_depart_edges = int(pd.to_numeric(df.get("unique_depart_edges", pd.Series(dtype=float)), errors="coerce").fillna(0).max()) if "unique_depart_edges" in df.columns else 0
        unique_arrival_edges = int(pd.to_numeric(df.get("unique_arrival_edges", pd.Series(dtype=float)), errors="coerce").fillna(0).max()) if "unique_arrival_edges" in df.columns else 0
        unique_route_edges = int(pd.to_numeric(df.get("unique_route_edges", pd.Series(dtype=float)), errors="coerce").fillna(0).max()) if "unique_route_edges" in df.columns else 0
        coverage_ratio = float(pd.to_numeric(df.get("network_edge_coverage_ratio", pd.Series(dtype=float)), errors="coerce").fillna(0).max()) if "network_edge_coverage_ratio" in df.columns else 0.0
        key_metric = "coverage_ratio" if "network_edge_coverage_ratio" in df.columns else "generated_vehicle_count"
        key_value = str(coverage_ratio if "network_edge_coverage_ratio" in df.columns else generated_vehicle_count)
        note_parts.extend(
            [
                f"generated_vehicle_count={generated_vehicle_count}",
                f"road_group_count={road_group_count}",
                f"road_allocated_sum={road_allocated_sum}",
                f"unique_depart_edges={unique_depart_edges}",
                f"unique_arrival_edges={unique_arrival_edges}",
                f"unique_route_edges={unique_route_edges}",
                f"coverage_ratio={coverage_ratio}",
                f"scenario_name={scenario_name}",
            ]
        )
    elif name in {"demand_source_audit.csv", "route_generation_audit.csv"}:
        edge_count = int(df["edge_id"].astype(str).nunique()) if "edge_id" in df.columns else 0
        volume_sum = float(pd.to_numeric(df.get("volume", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if "volume" in df.columns else 0.0
        confidence = str(df["confidence_level"].mode().iloc[0]) if "confidence_level" in df.columns and not df.empty else ""
        key_metric = "volume_sum"
        key_value = str(round(volume_sum, 3))
        note_parts.extend(
            [
                f"edge_count={edge_count}",
                f"volume_sum={round(volume_sum, 3)}",
                f"confidence_level={confidence}",
            ]
        )
    elif name.startswith("pedestrian_") or name in {"invalid_pedestrian_candidates.csv", "invalid_pedestrian_routes.csv", "skipped_pedestrian_routes.csv"}:
        crosswalk_count = int(df["crosswalk_id"].astype(str).nunique()) if "crosswalk_id" in df.columns else 0
        generated_count = int(pd.to_numeric(df.get("generated_pedestrian_count", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if "generated_pedestrian_count" in df.columns else 0
        invalid_count = int(len(df))
        dominant = ""
        share = 0.0
        if "crosswalk_id" in df.columns and not df.empty:
            counts = df["crosswalk_id"].astype(str).value_counts()
            dominant = str(counts.index[0])
            share = float(counts.iloc[0]) / float(max(int(counts.sum()), 1))
        key_metric = "generated_pedestrian_count"
        key_value = str(generated_count)
        note_parts.extend(
            [
                f"crosswalk_count={crosswalk_count}",
                f"generated_pedestrian_count={generated_count}",
                f"invalid_row_count={invalid_count}",
                f"dominant_crosswalk_id={dominant}",
                f"dominant_crosswalk_share={round(share, 6)}",
            ]
        )
    else:
        note_parts.extend([f"rows={row_count}", f"columns={column_count}"])

    return {
        "row_count": row_count,
        "column_count": column_count,
        "key_metric": key_metric,
        "key_value": key_value,
        "note": "; ".join(note_parts),
    }


def _summary_for_md(path: Path) -> dict[str, Any]:
    text = _safe_read_text(path)
    line_count = len(text.splitlines())
    heading_count = sum(1 for line in text.splitlines() if line.lstrip().startswith("#"))
    first_heading = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            first_heading = stripped.lstrip("#").strip()
            break
    return {
        "row_count": line_count,
        "column_count": heading_count,
        "key_metric": "first_heading",
        "key_value": first_heading,
        "note": f"line_count={line_count}; heading_count={heading_count}",
    }


def _summary_for_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(_safe_read_text(path))
    except Exception:
        return {"row_count": 0, "column_count": 0, "key_metric": "json", "key_value": "unreadable", "note": "unreadable_json"}
    if isinstance(data, dict):
        keys = len(data)
        kind = "dict"
    elif isinstance(data, list):
        keys = len(data)
        kind = "list"
    else:
        keys = 1
        kind = type(data).__name__
    return {
        "row_count": keys,
        "column_count": 0,
        "key_metric": "json_kind",
        "key_value": kind,
        "note": f"json_kind={kind}; item_count={keys}",
    }


def _summarize_file(path: Path, summary_category: str) -> dict[str, Any]:
    suffix = "".join(path.suffixes).lower()
    if suffix.endswith(".csv") or path.suffix.lower() == ".csv":
        return _summary_for_csv(path, summary_category)
    if suffix.endswith(".md") or path.suffix.lower() == ".md":
        return _summary_for_md(path)
    if suffix.endswith(".json") or path.suffix.lower() == ".json":
        return _summary_for_json(path)
    return {
        "row_count": "",
        "column_count": "",
        "key_metric": "",
        "key_value": "",
        "note": "",
    }


def _target_path(run_dir: Path, source_path: Path, bucket: str) -> Path:
    relative = source_path.relative_to(run_dir)
    if relative.parts and relative.parts[0] == "outputs":
        return run_dir / "log" / bucket / relative
    if relative.parts and relative.parts[0] == "sumo_nets":
        return run_dir / "log" / bucket / relative
    return run_dir / "log" / bucket / relative


def _discover_source_files(run_dir: Path) -> list[Path]:
    sources: list[Path] = []
    outputs_dir = run_dir / "outputs"
    sumo_dir = run_dir / "sumo_nets"
    for path in sorted(outputs_dir.rglob("*")) if outputs_dir.exists() else []:
        if path.is_file():
            sources.append(path)
    for path in sorted(sumo_dir.rglob("*")) if sumo_dir.exists() else []:
        if path.is_file():
            sources.append(path)
    for path in sorted(run_dir.iterdir()):
        if path.is_file() and path.name not in {"pipeline_freshness_audit.csv", "pipeline_freshness_audit.md"} and path.name != ".DS_Store":
            if path.name == "run_metadata.json":
                sources.append(path)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in sources:
        key = str(path.resolve())
        if key not in seen and "csv" not in path.parts and "log" not in path.parts:
            seen.add(key)
            unique.append(path)
    return unique


def _summary_category_display(category: str) -> str:
    return category if category in SUMMARY_CATEGORIES else "other"


def build_catalog(run_dir: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for source_path in _discover_source_files(run_dir):
        summary_category, reason = _classify_summary_category(source_path)
        log_bucket = _classify_log_bucket(source_path)
        artifact_type = "file"
        if source_path.suffix.lower() == ".md":
            artifact_type = "markdown"
        elif source_path.suffix.lower() == ".csv":
            artifact_type = "csv"
        elif source_path.suffix.lower() == ".json":
            artifact_type = "json"
        elif source_path.suffix.lower() in {".xml", ".sumocfg", ".log"} or source_path.name.endswith(".net.xml"):
            artifact_type = "sumo" if log_bucket == "sumo" else source_path.suffix.lower().lstrip(".")
        summary = _summarize_file(source_path, summary_category)
        rows.append(
            ArtifactRecord(
                source_path=source_path,
                relative_path=str(source_path.relative_to(run_dir)),
                summary_category=summary_category,
                log_bucket=log_bucket,
                action="copy",
                target_path=str(_target_path(run_dir, source_path, log_bucket)),
                reason=reason,
                artifact_type=artifact_type,
                include_in_summary=summary_category in {"traffic", "safety", "other"},
                row_count=summary.get("row_count", ""),
                column_count=summary.get("column_count", ""),
                key_metric=summary.get("key_metric", ""),
                key_value=summary.get("key_value", ""),
                note=summary.get("note", ""),
            ).to_dict()
        )
    catalog = pd.DataFrame(rows)
    if not catalog.empty:
        catalog = catalog.sort_values(["summary_category", "log_bucket", "relative_path"], kind="stable").reset_index(drop=True)
    return catalog


def _generate_observed_tables(catalog: pd.DataFrame) -> dict[str, pd.DataFrame]:
    traffic = catalog[catalog["summary_category"] == "traffic"].copy() if not catalog.empty else pd.DataFrame()
    safety = catalog[catalog["summary_category"] == "safety"].copy() if not catalog.empty else pd.DataFrame()
    other = catalog[catalog["summary_category"] == "other"].copy() if not catalog.empty else pd.DataFrame()
    return {"traffic": traffic, "safety": safety, "other": other}


def _final_table(df: pd.DataFrame, summary_category: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=[
            "summary_category",
            "source_file",
            "log_bucket",
            "row_count",
            "column_count",
            "key_metric",
            "key_value",
            "note",
        ])
    out = df[[
        "summary_category",
        "relative_path",
        "log_bucket",
        "row_count",
        "column_count",
        "key_metric",
        "key_value",
        "note",
    ]].copy()
    out = out.rename(columns={"relative_path": "source_file"})
    out["summary_category"] = summary_category
    return out


def _render_markdown_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_empty_"
    try:
        return df.to_markdown(index=False)
    except Exception:
        try:
            return df.to_string(index=False)
        except Exception:
            header = ", ".join(df.columns.tolist())
            lines = [header]
            for _, row in df.iterrows():
                lines.append(", ".join(str(row.get(col, "")) for col in df.columns))
            return "\n".join(lines)


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def write_outputs(run_dir: Path, catalog: pd.DataFrame, apply: bool) -> dict[str, Any]:
    csv_dir = run_dir / "csv"
    log_dir = run_dir / "log"
    csv_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    plan = catalog.copy()
    plan["apply_requested"] = int(bool(apply))
    plan["planned_action"] = plan["action"]
    plan["summary_bucket"] = plan["summary_category"]
    plan_path = log_dir / "move_plan.csv"
    plan.to_csv(plan_path, index=False, encoding="utf-8-sig")

    cleanup_lines = [
        "# Cleanup Report",
        "",
        f"- Run dir: `{run_dir}`",
        f"- Apply requested: `{apply}`",
        f"- Source files discovered: `{len(catalog)}`",
        f"- Traffic artifacts: `{int((catalog['summary_category'] == 'traffic').sum()) if not catalog.empty else 0}`",
        f"- Safety artifacts: `{int((catalog['summary_category'] == 'safety').sum()) if not catalog.empty else 0}`",
        f"- Other artifacts: `{int((catalog['summary_category'] == 'other').sum()) if not catalog.empty else 0}`",
        f"- Log candidates: `{int((catalog['summary_category'] == 'log_candidate').sum()) if not catalog.empty else 0}`",
        f"- Excel status: `{'not_attempted' if not apply else 'pending'}`",
        "",
        "## Policy",
        "",
        "- Existing outputs are preserved.",
        "- Source files are copied into `log/` when `--apply` is used.",
        "- Canonical summary tables are generated under `csv/` when `--apply` is used.",
        "",
        "## Planned Targets",
        "",
    ]
    cleanup_lines.append(_render_markdown_table(plan[["relative_path", "summary_category", "log_bucket", "planned_action", "target_path", "reason"]]))
    cleanup_report_path = log_dir / "cleanup_report.md"
    cleanup_report_path.write_text("\n".join(cleanup_lines) + "\n", encoding="utf-8-sig")

    outputs: dict[str, Any] = {
        "move_plan": plan_path,
        "cleanup_report": cleanup_report_path,
    }

    if not apply:
        return outputs

    # Copy source artifacts into log buckets.
    for row in catalog.itertuples(index=False):
        source_path = Path(row.source_path)
        if not source_path.exists():
            continue
        target_path = Path(row.target_path)
        _copy_file(source_path, target_path)

    tables = _generate_observed_tables(catalog)
    traffic_df = _final_table(tables["traffic"], "traffic")
    safety_df = _final_table(tables["safety"], "safety")
    other_df = _final_table(tables["other"], "other")

    traffic_path = csv_dir / "observed_traffic_metrics.csv"
    safety_path = csv_dir / "observed_safety_metrics.csv"
    other_path = csv_dir / "observed_other_metrics.csv"
    catalog_path = csv_dir / "metric_catalog.csv"
    report_path = csv_dir / "csv_mapping_report.md"
    xlsx_path = csv_dir / "observed_metrics.xlsx"
    xlsx_status = "not_attempted"

    traffic_df.to_csv(traffic_path, index=False, encoding="utf-8-sig")
    safety_df.to_csv(safety_path, index=False, encoding="utf-8-sig")
    other_df.to_csv(other_path, index=False, encoding="utf-8-sig")
    catalog.to_csv(catalog_path, index=False, encoding="utf-8-sig")

    report_lines = [
        "# CSV Mapping Report",
        "",
        f"- Run dir: `{run_dir}`",
        f"- Generated at: `{_now_text()}`",
        "",
        "## Summary",
        "",
        f"- Traffic rows: `{len(traffic_df)}`",
        f"- Safety rows: `{len(safety_df)}`",
        f"- Other rows: `{len(other_df)}`",
        f"- Total catalog rows: `{len(catalog)}`",
        "",
        "## Notes",
        "",
        "- `csv/` contains canonical researcher-facing summaries.",
        "- `log/` contains copied raw, debug, audit, and SUMO artifacts.",
        "- Original `outputs/` files remain untouched.",
        "",
        "## Canonical CSVs",
        "",
        f"- `{traffic_path.relative_to(run_dir)}`",
        f"- `{safety_path.relative_to(run_dir)}`",
        f"- `{other_path.relative_to(run_dir)}`",
        f"- `{catalog_path.relative_to(run_dir)}`",
    ]
    report_lines.extend(
        [
            "",
            "## Traffic",
            "",
            _render_markdown_table(traffic_df),
            "",
            "## Safety",
            "",
            _render_markdown_table(safety_df),
            "",
            "## Other",
            "",
            _render_markdown_table(other_df),
        ]
    )

    xlsx_status = "not_attempted"
    try:
        with pd.ExcelWriter(xlsx_path) as writer:
            traffic_df.to_excel(writer, sheet_name="traffic", index=False)
            safety_df.to_excel(writer, sheet_name="safety", index=False)
            other_df.to_excel(writer, sheet_name="other", index=False)
        xlsx_status = "created"
    except Exception as exc:
        xlsx_status = f"failed:{exc.__class__.__name__}"
        if xlsx_path.exists():
            xlsx_path.unlink(missing_ok=True)  # type: ignore[arg-type]

    for idx, line in enumerate(cleanup_lines):
        if line.startswith("- Excel status:"):
            cleanup_lines[idx] = f"- Excel status: `{xlsx_status}`"
            break
    cleanup_report_path.write_text("\n".join(cleanup_lines) + "\n", encoding="utf-8-sig")

    report_lines_with_status = list(report_lines)
    for idx, line in enumerate(report_lines_with_status):
        if line.startswith(f"- Total catalog rows:"):
            report_lines_with_status.insert(idx + 1, f"- Excel status: `{xlsx_status}`")
            break
    else:
        report_lines_with_status.append(f"- Excel status: `{xlsx_status}`")
    report_path.write_text("\n".join(report_lines_with_status) + "\n", encoding="utf-8-sig")

    outputs.update(
        {
            "observed_traffic_metrics": traffic_path,
            "observed_safety_metrics": safety_path,
            "observed_other_metrics": other_path,
            "metric_catalog": catalog_path,
            "csv_mapping_report": report_path,
            "xlsx_status": xlsx_status,
            "observed_metrics_xlsx": xlsx_path if xlsx_status == "created" else None,
        }
    )
    return outputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Organize a single run directory into canonical csv/log structure.")
    parser.add_argument("--run_dir", required=True, help="Path to result/<run_name>")
    parser.add_argument("--apply", action="store_true", help="Copy artifacts into log/ and generate canonical csv/ tables.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_dir = Path(args.run_dir)
    if not run_dir.exists():
        raise FileNotFoundError(f"run_dir not found: {run_dir}")
    catalog = build_catalog(run_dir)
    outputs = write_outputs(run_dir, catalog, apply=args.apply)
    print(f"run_dir: {run_dir}")
    print(f"apply: {args.apply}")
    print(f"discovered_files: {len(catalog)}")
    for key, value in outputs.items():
        if isinstance(value, Path) and str(value):
            print(f"{key}: {value}")


if __name__ == "__main__":
    main()
