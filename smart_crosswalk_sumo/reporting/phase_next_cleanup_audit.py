#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


TIMESTAMP_RE = re.compile(r"^(?P<prefix>.+)_(?P<stamp>\d{8}_\d{6}|YYYYMMDD_HHMMSS)$")
LOG_NAME_RE = re.compile(r"(stdout|stderr|runtime_trace|debug_trace|trace|netconvert|sumo_.*\.log|\.log$)", re.IGNORECASE)
RAW_HEAVY_RE = re.compile(r"(edge_data_seed\d+\.xml|lane_data_seed\d+\.xml|tripinfo|fcd|emission|rou\.xml$|demand_.*\.rou\.xml$|route_.*\.xml$)", re.IGNORECASE)
SUMMARY_NAME_RE = re.compile(r"(summary|README|manifest|inventory|mapping|location|verdict|candidate|candidates|final|phase6|top50)", re.IGNORECASE)

EXPLICIT_PROTECTED_ROOTS = {
    "phase_next_top50_final_candidate_screening",
    "phase_next_top50_mapping_visual_audit_20260515_174626",
    "phase_next_top50_bad_location_generation_audit_20260515_180050",
    "phase_next_bad22_generated_crossing_patch_20260515_181150",
    "phase_next_generated_ok6_phase6_prep_20260515_182319",
    "phase6_vehicle_experiment_300_30seed_human",
    "phase_next_recovery_command_plan_20260514_200908",
    "phase6_transition_after_recovery_20260514_220549",
}

EXPLICIT_PROTECTED_FILES = {
    "phase6_ready_candidates_total_FINAL_237.csv",
}

SUMMARY_DOCS_FOR_RECENT_REF = [
    "docs/phase6_next_work_plan.md",
    "docs/result_cleanup_keep_list.md",
    "docs/result_cleanup_plan.md",
]


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _collect_corpus(paths: list[Path]) -> str:
    parts: list[str] = []
    for path in paths:
        if path.is_file():
            parts.append(_read_text(path).lower())
    return "\n".join(parts)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git_tracked_result_files(repo_root: Path) -> set[str]:
    proc = subprocess.run(
        ["git", "ls-files", "result"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    tracked: set[str] = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            tracked.add(line.replace("\\", "/"))
    return tracked


def _build_code_corpus(repo_root: Path) -> str:
    sources: list[Path] = []
    for path in repo_root.rglob("*.py"):
        if "result" in path.parts:
            continue
        sources.append(path)
    for path in repo_root.rglob("*.md"):
        if "result" in path.parts:
            continue
        sources.append(path)
    for path in repo_root.glob("*.sh"):
        sources.append(path)
    if (repo_root / "README.md").is_file():
        sources.append(repo_root / "README.md")
    return _collect_corpus(sorted(set(sources)))


def _build_recent_summary_corpus(repo_root: Path) -> str:
    sources: list[Path] = []
    for rel in SUMMARY_DOCS_FOR_RECENT_REF:
        path = repo_root / rel
        if path.is_file():
            sources.append(path)
    for root_name in [
        "phase6_transition_after_recovery_20260514_220549",
        "phase_next_recovery_command_plan_20260514_200908",
        "phase_next_top50_final_candidate_screening",
        "phase_next_top50_mapping_visual_audit_20260515_174626",
        "phase_next_top50_bad_location_generation_audit_20260515_180050",
        "phase_next_bad22_generated_crossing_patch_20260515_181150",
        "phase_next_generated_ok6_phase6_prep_20260515_182319",
        "phase6_vehicle_experiment_300_30seed_human",
    ]:
        root = repo_root / "result" / root_name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and (path.suffix == ".md" or path.name.endswith(".summary.md") or path.name.endswith("_summary.md")):
                sources.append(path)
    return _collect_corpus(sorted(set(sources)))


def _safe_modified_time(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except Exception:
        return ""


def _reference_terms(path: Path) -> list[str]:
    terms = [path.name, path.stem, path.parent.name]
    try:
        parts = path.relative_to(_repo_root() / "result").parts
        if parts:
            terms.append(parts[0])
    except Exception:
        pass
    return [term.lower() for term in terms if term and len(term) >= 3]


def _referenced_by(corpus: str, path: Path) -> bool:
    rel = path.as_posix().lower()
    if rel in corpus:
        return True
    for term in _reference_terms(path):
        if term in corpus:
            return True
    return False


def _top_level_root(rel_path: str) -> str:
    return rel_path.split("/", 1)[0]


def _version_prefix(root_name: str) -> tuple[str, str] | None:
    match = TIMESTAMP_RE.match(root_name)
    if not match:
        return None
    return match.group("prefix"), match.group("stamp")


def _collect_latest_versions(result_root: Path) -> dict[str, str]:
    latest: dict[str, str] = {}
    for path in result_root.iterdir():
        if not path.is_dir():
            continue
        parsed = _version_prefix(path.name)
        if not parsed:
            continue
        prefix, stamp = parsed
        if stamp == "YYYYMMDD_HHMMSS":
            continue
        if prefix not in latest or stamp > latest[prefix]:
            latest[prefix] = stamp
    return latest


def _duplicate_version_reason(root_name: str, latest_versions: dict[str, str]) -> str:
    parsed = _version_prefix(root_name)
    if not parsed:
        return ""
    prefix, stamp = parsed
    latest = latest_versions.get(prefix)
    if not latest:
        return ""
    if stamp == "YYYYMMDD_HHMMSS":
        return f"placeholder timestamp for {prefix}; keep latest {latest}"
    if stamp < latest:
        return f"older version of {prefix}; latest is {latest}"
    return ""


def _classify_likely_role(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".sumocfg"):
        return "sumo_config"
    if name.endswith(".net.xml"):
        return "network_xml"
    if name.endswith(".rou.xml"):
        return "route_xml"
    if name.endswith(".xml"):
        return "xml_output"
    if name.endswith(".csv"):
        if "candidate" in name or "manifest" in name or "inventory" in name or "mapping" in name or "location" in name or "verdict" in name or "final" in name or "phase6" in name or "top50" in name:
            return "candidate_or_result_csv"
        if "summary" in name:
            return "summary_csv"
        if "log" in name or "debug" in name or "trace" in name:
            return "diagnostic_csv"
        return "csv_output"
    if name.endswith(".json"):
        if "metadata" in name or "manifest" in name:
            return "run_metadata_or_manifest"
        if "debug" in name or "trace" in name:
            return "diagnostic_json"
        return "json_output"
    if name.endswith(".md"):
        if "readme" in name:
            return "readme"
        if "summary" in name:
            return "summary_md"
        return "markdown"
    if name.endswith(".log"):
        return "log_output"
    if name.endswith(".png") or name.endswith(".jpg") or name.endswith(".jpeg") or name.endswith(".svg"):
        return "image_or_figure"
    if name.endswith(".sh"):
        return "shell_script"
    return "intermediate_output"


def _protected_reason(
    path: Path,
    rel_path: str,
    code_corpus: str,
    recent_summary_corpus: str,
    git_tracked: bool,
) -> list[str]:
    reasons: list[str] = []
    parts = path.parts
    top = _top_level_root(rel_path)
    if top in EXPLICIT_PROTECTED_ROOTS:
        reasons.append(f"explicit protected root: {top}")
    if any(part == "old" for part in parts):
        reasons.append("result/old archive area")
    if path.name in EXPLICIT_PROTECTED_FILES:
        reasons.append(f"explicit protected file: {path.name}")
    if path.name.startswith("README") or "README" in path.name:
        reasons.append("README artifact")
    if path.name.endswith(".net.xml"):
        reasons.append(".net.xml protected")
    if path.name.endswith(".sumocfg"):
        reasons.append(".sumocfg protected")
    if path.name.startswith("command_to_run") or path.name.startswith("command_to_build") or path.name.endswith(".sh"):
        reasons.append("run/build script protected")
    if path.name.endswith(".md") and SUMMARY_NAME_RE.search(path.name):
        reasons.append("summary markdown")
    if path.name.endswith(".csv") and SUMMARY_NAME_RE.search(path.name):
        reasons.append("candidate/summary CSV")
    if git_tracked:
        reasons.append("git tracked")
    if _referenced_by(code_corpus, path):
        reasons.append("referenced by code/docs")
    if _referenced_by(recent_summary_corpus, path):
        reasons.append("referenced by recent summary")
    return reasons


def _cleanup_status(
    path: Path,
    rel_path: str,
    protected_reasons: list[str],
    duplicate_reason: str,
    code_corpus: str,
) -> tuple[str, str]:
    name = path.name.lower()
    parent = path.parent.as_posix().lower()

    if protected_reasons:
        return "protected", "; ".join(protected_reasons)

    if duplicate_reason:
        return "duplicate_old_version", duplicate_reason

    if any(part == "old" for part in path.parts):
        return "review_needed", "result/old archive area needs manual review"

    if LOG_NAME_RE.search(name):
        return "log_only", "log/debug/trace artifact"

    if RAW_HEAVY_RE.search(name):
        return "generated_raw_heavy", "raw simulation/output artifact"

    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg"}:
        return "generated_raw_heavy", "temporary image/figure"

    if "smoke" in name or "smoke" in parent or "verify" in name or "audit" in name or "reverification" in name:
        return "safe_to_archive", "smoke/verification/audit intermediate output"

    if "cleanup" in name or "cleanup" in parent:
        return "safe_to_archive", "prior cleanup artifact"

    if "tmp" in name or "temp" in name or "cache" in name or name.startswith(".ds_store"):
        return "safe_to_archive", "temporary artifact"

    if path.suffix.lower() in {".csv", ".json", ".md"} and not _referenced_by(code_corpus, path):
        return "review_needed", "unreferenced structured artifact"

    return "unknown_keep", "no strong archive signal"


def _scan_inventory(result_root: Path, exclude_root: Path | None = None) -> pd.DataFrame:
    repo_root = _repo_root()
    code_corpus = _build_code_corpus(repo_root)
    recent_summary_corpus = _build_recent_summary_corpus(repo_root)
    git_tracked = _git_tracked_result_files(repo_root)
    latest_versions = _collect_latest_versions(result_root)

    rows: list[dict[str, Any]] = []
    for path in sorted(result_root.rglob("*")):
        if not path.is_file():
            continue
        if exclude_root and _is_under(path, exclude_root):
            continue
        rel_path = path.relative_to(result_root).as_posix()
        top = _top_level_root(rel_path)
        duplicate_reason = _duplicate_version_reason(top, latest_versions)
        git_rel = rel_path if rel_path.startswith("result/") else f"result/{rel_path}"
        tracked = git_rel in git_tracked
        protected_reasons = _protected_reason(path, rel_path, code_corpus, recent_summary_corpus, tracked)
        cleanup_status, cleanup_reason = _cleanup_status(path, rel_path, protected_reasons, duplicate_reason, code_corpus)
        rows.append(
            {
                "file_path": rel_path,
                "file_size_mb": round(path.stat().st_size / (1024 * 1024), 6),
                "modified_time": _safe_modified_time(path),
                "extension": "".join(path.suffixes) if path.suffixes else "",
                "parent_dir": path.parent.as_posix(),
                "git_tracked": bool(tracked),
                "likely_role": _classify_likely_role(path),
                "referenced_by_code": bool(_referenced_by(code_corpus, path)),
                "referenced_by_recent_summary": bool(_referenced_by(recent_summary_corpus, path)),
                "cleanup_status": cleanup_status,
                "protected_reason": "; ".join(protected_reasons),
                "cleanup_reason": cleanup_reason,
            }
        )

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["file_path"]).reset_index(drop=True)
    return df


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    _ensure_dir(path.parent)
    df.to_csv(path, index=False, encoding="utf-8")


def _format_mb(value: float) -> str:
    return f"{value:.2f} MB"


def _group_dir_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["dir_path", "file_count", "total_mb"])
    group = df.copy()
    group["dir_path"] = group["file_path"].str.split("/").str[0]
    out = (
        group.groupby("dir_path", as_index=False)
        .agg(file_count=("file_path", "count"), total_mb=("file_size_mb", "sum"))
        .sort_values(["total_mb", "file_count"], ascending=[False, False])
        .reset_index(drop=True)
    )
    return out


def _write_summary_md(output_dir: Path, df: pd.DataFrame, result_root: Path) -> None:
    total_files = len(df)
    total_mb = float(df["file_size_mb"].sum()) if not df.empty else 0.0

    counts = df.groupby("cleanup_status", dropna=False)["file_path"].count().to_dict() if not df.empty else {}
    sizes = df.groupby("cleanup_status", dropna=False)["file_size_mb"].sum().to_dict() if not df.empty else {}
    safe_statuses = ["safe_to_archive", "duplicate_old_version", "generated_raw_heavy", "log_only"]
    safe_count = int(sum(counts.get(status, 0) for status in safe_statuses))
    safe_size = float(sum(sizes.get(status, 0.0) for status in safe_statuses))

    lines = [
        "# Phase Next Cleanup Audit",
        "",
        f"- created_at: `{datetime.now(timezone.utc).isoformat(timespec='seconds')}`",
        f"- result_root: `{result_root}`",
        f"- total_files: `{total_files}`",
        f"- total_size: `{_format_mb(total_mb)}`",
        f"- protected_files: `{int(counts.get('protected', 0))}`",
        f"- protected_size: `{_format_mb(float(sizes.get('protected', 0.0)))}`",
        f"- safe_to_archive_files: `{safe_count}`",
        f"- safe_to_archive_size: `{_format_mb(safe_size)}`",
        f"- review_needed_files: `{int(counts.get('review_needed', 0))}`",
        f"- review_needed_size: `{_format_mb(float(sizes.get('review_needed', 0.0)))}`",
        f"- unknown_keep_files: `{int(counts.get('unknown_keep', 0))}`",
        f"- unknown_keep_size: `{_format_mb(float(sizes.get('unknown_keep', 0.0)))}`",
        "",
        "## Biggest Files",
        "",
        "| file_path | size_mb | cleanup_status |",
        "|---|---:|---|",
    ]
    for _, row in df.sort_values("file_size_mb", ascending=False).head(30).iterrows():
        lines.append(f"| `{row['file_path']}` | {row['file_size_mb']:.3f} | {row['cleanup_status']} |")

    cleanup_df = df[df["cleanup_status"].isin(["safe_to_archive", "duplicate_old_version", "generated_raw_heavy", "log_only"])]
    lines += [
        "",
        "## Largest Cleanup Candidates",
        "",
        "| file_path | size_mb | cleanup_status | reason |",
        "|---|---:|---|---|",
    ]
    for _, row in cleanup_df.sort_values("file_size_mb", ascending=False).head(30).iterrows():
        reason = str(row["cleanup_reason"]).replace("|", "\\|")
        lines.append(f"| `{row['file_path']}` | {row['file_size_mb']:.3f} | {row['cleanup_status']} | {reason} |")

    safe_dirs = (
        cleanup_df.assign(top_dir=cleanup_df["file_path"].str.split("/").str[0])
        .groupby("top_dir", as_index=False)
        .agg(total_mb=("file_size_mb", "sum"), file_count=("file_path", "count"))
        .sort_values(["total_mb", "file_count"], ascending=[False, False])
    )
    protected_subset = df[df["cleanup_status"] == "protected"].copy()
    if not protected_subset.empty:
        protected_subset["top_dir"] = protected_subset["file_path"].str.split("/").str[0]
        protected_dirs = (
            protected_subset.groupby("top_dir", as_index=False)
            .agg(total_mb=("file_size_mb", "sum"), file_count=("file_path", "count"))
            .sort_values(["total_mb", "file_count"], ascending=[False, False])
        )
    else:
        protected_dirs = pd.DataFrame(columns=["top_dir", "total_mb", "file_count"])

    lines += [
        "",
        "## Likely Safe Folders",
        "",
        "| dir_path | file_count | total_mb |",
        "|---|---:|---:|",
    ]
    for _, row in safe_dirs.head(30).iterrows():
        lines.append(f"| `{row['top_dir']}` | {int(row['file_count'])} | {row['total_mb']:.3f} |")

    lines += [
        "",
        "## Do Not Touch",
        "",
        "| dir_path | file_count | total_mb |",
        "|---|---:|---:|",
    ]
    for _, row in protected_dirs.head(30).iterrows():
        lines.append(f"| `{row['top_dir']}` | {int(row['file_count'])} | {row['total_mb']:.3f} |")

    lines += [
        "",
        "## Next Run",
        "",
        "1. Review `protected_inventory.csv`, `safe_to_archive_inventory.csv`, and `review_needed_inventory.csv`.",
        "2. If safe, run `APPLY=1 bash command_to_quarantine_safe_to_archive.sh` from audit dir.",
        "3. If rollback needed, run `bash command_to_restore_quarantine.sh`.",
    ]
    (output_dir / "cleanup_audit_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_quarantine_script(output_dir: Path, result_root: Path) -> None:
    script = f"""#!/usr/bin/env bash
set -euo pipefail

AUDIT_DIR="{output_dir}"
RESULT_ROOT="{result_root}"
INVENTORY="$AUDIT_DIR/safe_to_archive_inventory.csv"
ARCHIVE_DIR="$RESULT_ROOT/_archive_unused_{_now_stamp()}"
MANIFEST="$ARCHIVE_DIR/quarantine_manifest.csv"

echo "Dry-run default. Review moves first."
echo "Target archive: $ARCHIVE_DIR"
echo "Use: APPLY=1 bash command_to_quarantine_safe_to_archive.sh"

if [[ ! -f "$INVENTORY" ]]; then
  echo "Missing inventory: $INVENTORY" >&2
  exit 1
fi

mapfile -t MOVE_ROWS < <(python3 - "$INVENTORY" "$RESULT_ROOT" "$ARCHIVE_DIR" "$MANIFEST" <<'PY'
import csv
import os
import sys
from pathlib import Path

inventory = Path(sys.argv[1])
result_root = Path(sys.argv[2])
archive_dir = Path(sys.argv[3])
manifest = Path(sys.argv[4])

rows = []
with inventory.open("r", encoding="utf-8", newline="") as f:
    reader = csv.DictReader(f)
    for row in reader:
        if row.get("cleanup_status") in {"safe_to_archive", "duplicate_old_version", "generated_raw_heavy", "log_only"}:
            src = result_root / row["file_path"]
            dst = archive_dir / row["file_path"]
            rows.append((src, dst, row["cleanup_status"], row.get("cleanup_reason", "")))

if os.environ.get("APPLY") == "1":
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["original_path", "archived_path", "cleanup_status", "cleanup_reason"])
        for src, dst, status, reason in rows:
            writer.writerow([str(src), str(dst), status, reason])

for src, dst, status, reason in rows:
    print("%s\\t%s\\t%s\\t%s" % (src, dst, status, reason))
PY
)

echo "planned_moves=${{#MOVE_ROWS[@]}}"
for row in "${{MOVE_ROWS[@]}}"; do
  IFS=$'\\t' read -r src dst status reason <<<"$row"
  [[ -n "${{src:-}}" ]] || continue
  if [[ "${{APPLY:-0}}" != "1" ]]; then
    echo "mv $src $dst [$status] $reason"
    continue
  fi
  if [[ ! -e "$src" ]]; then
    echo "skip_missing $src"
    continue
  fi
  mkdir -p "$(dirname "$dst")"
  mv "$src" "$dst"
done

if [[ "${{APPLY:-0}}" != "1" ]]; then
  echo "Dry-run only. No files moved."
  exit 0
fi
"""
    path = output_dir / "command_to_quarantine_safe_to_archive.sh"
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def _write_restore_script(output_dir: Path, result_root: Path) -> None:
    script = f"""#!/usr/bin/env bash
set -euo pipefail

AUDIT_DIR="{output_dir}"
RESULT_ROOT="{result_root}"
ARCHIVE_DIR="${{ARCHIVE_DIR:-}}"

echo "Restore uses quarantine manifest."
echo "Set ARCHIVE_DIR to exact archive folder if needed."

manifest=""
if [[ -n "${{ARCHIVE_DIR}}" && -f "${{ARCHIVE_DIR}}/quarantine_manifest.csv" ]]; then
  manifest="${{ARCHIVE_DIR}}/quarantine_manifest.csv"
else
  latest_manifest="$(ls -1dt "$RESULT_ROOT"/_archive_unused_*/quarantine_manifest.csv 2>/dev/null | head -n 1 || true)"
  manifest="${{latest_manifest:-}}"
fi

if [[ -z "$manifest" || ! -f "$manifest" ]]; then
  echo "No quarantine manifest found." >&2
  exit 1
fi

echo "Manifest: $manifest"
echo "Use: APPLY=1 bash command_to_restore_quarantine.sh"

if [[ "${{APPLY:-0}}" != "1" ]]; then
  echo "Dry-run only. No files moved."
  exit 0
fi

while IFS=$'\\t' read -r src dst; do
  [[ -n "${{src:-}}" ]] || continue
  if [[ ! -e "$src" ]]; then
    echo "skip_missing $src"
    continue
  fi
  mkdir -p "$(dirname "$dst")"
  mv "$src" "$dst"
done < <(python3 - "$manifest" <<'PY'
import csv
import sys
from pathlib import Path

manifest = Path(sys.argv[1])
with manifest.open("r", encoding="utf-8", newline="") as f:
    reader = csv.DictReader(f)
    for row in reader:
        print("%s\\t%s" % (row["archived_path"], row["original_path"]))
PY
)
"""
    path = output_dir / "command_to_restore_quarantine.sh"
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def build_audit(result_root: Path, output_dir: Path) -> pd.DataFrame:
    df = _scan_inventory(result_root, exclude_root=output_dir)
    _ensure_dir(output_dir)
    _write_csv(output_dir / "cleanup_inventory.csv", df)

    safe_df = df[df["cleanup_status"].isin(["safe_to_archive", "duplicate_old_version", "generated_raw_heavy", "log_only"])].copy()
    review_df = df[df["cleanup_status"] == "review_needed"].copy()
    protected_df = df[df["cleanup_status"] == "protected"].copy()

    _write_csv(output_dir / "safe_to_archive_inventory.csv", safe_df)
    _write_csv(output_dir / "review_needed_inventory.csv", review_df)
    _write_csv(output_dir / "protected_inventory.csv", protected_df)
    _write_summary_md(output_dir, df, result_root)
    _write_quarantine_script(output_dir, result_root)
    _write_restore_script(output_dir, result_root)
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase next cleanup audit. Dry-run only.")
    parser.add_argument("--result-root", default="result")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    result_root = Path(args.result_root).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else result_root / f"phase_next_cleanup_audit_{_now_stamp()}"
    df = build_audit(result_root, output_dir)

    counts = df.groupby("cleanup_status")["file_path"].count().to_dict() if not df.empty else {}
    sizes = df.groupby("cleanup_status")["file_size_mb"].sum().to_dict() if not df.empty else {}
    safe_statuses = ["safe_to_archive", "duplicate_old_version", "generated_raw_heavy", "log_only"]
    payload = {
        "output_dir": str(output_dir),
        "total_files": int(len(df)),
        "total_size_mb": round(float(df["file_size_mb"].sum()) if not df.empty else 0.0, 6),
        "protected_files": int(counts.get("protected", 0)),
        "protected_size_mb": round(float(sizes.get("protected", 0.0)), 6),
        "safe_to_archive_files": int(sum(counts.get(k, 0) for k in safe_statuses)),
        "safe_to_archive_size_mb": round(float(sum(sizes.get(k, 0.0) for k in safe_statuses)), 6),
        "review_needed_files": int(counts.get("review_needed", 0)),
        "review_needed_size_mb": round(float(sizes.get("review_needed", 0.0)), 6),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
