"""result 디렉터리 정리 계획 생성기 (dry-run only).

절대 삭제/이동/압축하지 않는다.
스캔 후 cleanup_plan.csv + cleanup_plan.md만 생성한다.

CLI:
  python3 -m smart_crosswalk_sumo.reporting.result_cleanup_planner \\
    --result_root result \\
    --output_csv result/cleanup_plan.csv \\
    --keep_run_dir result/phase5c_diag_119055 \\
    --keep_final_package result/final_package_phase6b_119055
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# 카테고리 정의
# ---------------------------------------------------------------------------

_CATEGORY_DESC = {
    "KEEP_FINAL_PACKAGE":          "최종 패키지 — 유지 필수",
    "KEEP_CURRENT_RUN":            "현재 사용 중인 실행 결과 — 유지 필수",
    "KEEP_SOURCE_OR_REGISTRY":     "소스 데이터 또는 레지스트리 — 유지 필수",
    "KEEP_SAFETY_REPORT":          "안전 보고서 — 유지 필수",
    "REVIEW_LARGE":                "대용량 파일 — 검토 후 결정",
    "REVIEW_DUPLICATE_NETWORK":    "중복 네트워크 파일 — 검토 후 결정",
    "REVIEW_FAILED_SMOKE":         "실패하거나 부분 완료된 실행 — 검토 후 결정",
    "REVIEW_TEMP_LOGS":            "임시 로그 파일 — 검토 후 결정",
    "REVIEW_OLD_DIAGNOSTIC":       "오래된 진단/smoke 결과 — 검토 후 결정",
    "SAFE_TO_DELETE_AFTER_BACKUP": "백업 후 삭제 가능",
    "DO_NOT_DELETE":               "삭제 금지 (소스 코드, 문서)",
    "UNKNOWN_REVIEW":              "분류 미정 — 검토 필요",
}

_SUGGESTED_ACTIONS = {
    "KEEP_FINAL_PACKAGE":          "유지",
    "KEEP_CURRENT_RUN":            "유지",
    "KEEP_SOURCE_OR_REGISTRY":     "유지",
    "KEEP_SAFETY_REPORT":          "유지",
    "REVIEW_LARGE":                "검토 후 압축 또는 백업 이동 고려",
    "REVIEW_DUPLICATE_NETWORK":    "중복 여부 확인 후 백업 이동 고려",
    "REVIEW_FAILED_SMOKE":         "완료 여부 확인 후 정리 고려",
    "REVIEW_TEMP_LOGS":            "필요 없으면 백업 후 삭제 가능",
    "REVIEW_OLD_DIAGNOSTIC":       "최신 결과로 대체됐으면 백업 후 삭제 가능",
    "SAFE_TO_DELETE_AFTER_BACKUP": "백업 확인 후 삭제 가능",
    "DO_NOT_DELETE":               "유지",
    "UNKNOWN_REVIEW":              "내용 확인 후 결정",
}

_LARGE_FILE_THRESHOLD = 10 * 1024 * 1024  # 10 MB

_REGISTRY_FILENAMES = {
    "T2_crosswalk_features.csv",
    "taas_calibration_targets.json",
    "junggu_crosswalk_sumo_registry_final.csv",
    "runnable_crosswalk_ids_final.csv",
    "final_registry_summary.csv",
}

_NOTABLE_FILE_SUFFIXES = (".net.xml", ".osm", ".trips.xml", ".rou.xml")
_TEMP_LOG_PREFIXES = ("sumo_stderr_", "sumo_stdout_", "netconvert", "runtime_trace")

# ---------------------------------------------------------------------------
# 크기 계산
# ---------------------------------------------------------------------------

def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except Exception:
        return 0


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except Exception:
                    pass
    except Exception:
        pass
    return total


# ---------------------------------------------------------------------------
# 분류 로직
# ---------------------------------------------------------------------------

def _is_under(path: Path, parent: Path | None) -> bool:
    if parent is None:
        return False
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _categorize(
    path: Path,
    is_dir: bool,
    size_bytes: int,
    keep_run_dir: Path | None,
    keep_final_package: Path | None,
) -> tuple[str, str]:
    name = path.name
    path_str = str(path)

    # Explicit keep_final_package
    if keep_final_package and (
        path == keep_final_package or _is_under(path, keep_final_package)
    ):
        return "KEEP_FINAL_PACKAGE", f"--keep_final_package 지정: {keep_final_package.name}"

    # Explicit keep_run_dir
    if keep_run_dir and (
        path == keep_run_dir or _is_under(path, keep_run_dir)
    ):
        return "KEEP_CURRENT_RUN", f"--keep_run_dir 지정: {keep_run_dir.name}"

    # final_package_* dirs
    if is_dir and name.startswith("final_package_"):
        return "KEEP_FINAL_PACKAGE", "final_package_* 디렉터리"

    # Safety report dirs
    if is_dir and "safety_report" in name.lower():
        return "KEEP_SAFETY_REPORT", "safety report 디렉터리"
    if not is_dir and "safety_report" in path_str.lower():
        return "KEEP_SAFETY_REPORT", "safety report 파일"

    # Registry / source data files
    if name in _REGISTRY_FILENAMES:
        return "KEEP_SOURCE_OR_REGISTRY", "레지스트리 또는 소스 데이터 파일"
    if not is_dir and "registry" in name.lower() and name.endswith(".csv"):
        return "KEEP_SOURCE_OR_REGISTRY", "레지스트리 관련 CSV"

    # Old smoke_* dirs
    if is_dir and name.startswith("smoke_"):
        return "REVIEW_OLD_DIAGNOSTIC", "오래된 smoke 실행 결과"

    # Older phase diagnostic dirs (not current keep_run_dir)
    if is_dir and any(
        name.startswith(x) for x in [
            "phase4", "phase5c_diag", "phase6a", "phase5_diag",
            "phase5b", "phase3", "phase2",
        ]
    ):
        return "REVIEW_OLD_DIAGNOSTIC", "이전 단계 진단 실행 결과"

    # Temp logs
    if not is_dir and any(name.startswith(p) for p in _TEMP_LOG_PREFIXES):
        return "REVIEW_TEMP_LOGS", "SUMO/netconvert/런타임 로그 파일"

    # Duplicate network dirs
    if is_dir and name == "sumo_nets":
        return "REVIEW_DUPLICATE_NETWORK", "SUMO 네트워크 디렉터리 (중복 가능)"

    # Duplicate network files
    if not is_dir and name.endswith(".net.xml"):
        return "REVIEW_DUPLICATE_NETWORK", "SUMO 네트워크 파일 (중복 가능)"
    if not is_dir and name.endswith(".osm"):
        return "REVIEW_DUPLICATE_NETWORK", "OSM 원본 데이터 (중복 가능)"

    # Large files
    if not is_dir and size_bytes > _LARGE_FILE_THRESHOLD:
        return "REVIEW_LARGE", f"대용량 파일 ({size_bytes / 1024 / 1024:.1f} MB)"

    return "UNKNOWN_REVIEW", "분류 미정"


# ---------------------------------------------------------------------------
# 스캔
# ---------------------------------------------------------------------------

def _scan(
    result_root: Path,
    keep_run_dir: Path | None,
    keep_final_package: Path | None,
) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()

    def add(path: Path, is_dir: bool) -> None:
        ps = str(path)
        if ps in seen:
            return
        seen.add(ps)
        size_bytes = _dir_size(path) if is_dir else _file_size(path)
        cat, reason = _categorize(path, is_dir, size_bytes, keep_run_dir, keep_final_package)
        rows.append({
            "path":             str(path),
            "type":             "directory" if is_dir else "file",
            "size_bytes":       size_bytes,
            "size_mb":          round(size_bytes / 1024 / 1024, 3),
            "category":         cat,
            "reason":           reason,
            "suggested_action": _SUGGESTED_ACTIONS.get(cat, "검토 필요"),
        })

    if not result_root.exists():
        return rows

    # Top-level entries
    for entry in sorted(result_root.iterdir()):
        if entry.is_dir():
            add(entry, True)
            # Notable files within directory
            try:
                for sub in sorted(entry.rglob("*")):
                    if not sub.is_file():
                        continue
                    sz = _file_size(sub)
                    notable = (
                        sz > _LARGE_FILE_THRESHOLD
                        or sub.name.endswith(_NOTABLE_FILE_SUFFIXES)
                        or any(sub.name.startswith(p) for p in _TEMP_LOG_PREFIXES)
                    )
                    if notable:
                        add(sub, False)
            except Exception:
                pass
        elif entry.is_file():
            add(entry, False)

    return rows


# ---------------------------------------------------------------------------
# MD 요약
# ---------------------------------------------------------------------------

def _build_md(df: pd.DataFrame, result_root: Path) -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    lines = [
        "# result 디렉터리 정리 계획 (dry-run)",
        "",
        f"생성일시: {now}",
        f"스캔 대상: `{result_root}`",
        "",
        "> **주의**: 이 파일은 계획서입니다. 아무것도 삭제/이동/압축하지 않았습니다.",
        "",
        "---",
        "",
        "## 카테고리별 요약",
        "",
        "| 카테고리 | 항목 수 | 총 크기 (MB) | 설명 |",
        "|---|---:|---:|---|",
    ]

    for cat, desc in _CATEGORY_DESC.items():
        subset = df[df["category"] == cat]
        if subset.empty:
            continue
        count = len(subset)
        total_mb = subset["size_mb"].sum()
        lines.append(f"| {cat} | {count} | {total_mb:.1f} | {desc} |")

    lines += [
        "",
        "---",
        "",
        "## 상위 10개 대용량 항목",
        "",
        "| 경로 | 크기 (MB) | 카테고리 | 권장 조치 |",
        "|---|---:|---|---|",
    ]
    for _, row in df.nlargest(10, "size_mb").iterrows():
        lines.append(
            f"| `{row['path']}` | {row['size_mb']:.1f}"
            f" | {row['category']} | {row['suggested_action']} |"
        )

    lines += [
        "",
        "---",
        "",
        "## REVIEW 항목 (검토 필요)",
        "",
        "| 경로 | 크기 (MB) | 카테고리 | 이유 |",
        "|---|---:|---|---|",
    ]
    review = df[df["category"].str.startswith("REVIEW")]
    for _, row in review.sort_values("size_mb", ascending=False).iterrows():
        lines.append(
            f"| `{row['path']}` | {row['size_mb']:.1f}"
            f" | {row['category']} | {row['reason']} |"
        )

    lines += [
        "",
        "---",
        "",
        "## 확인 체크리스트",
        "",
        "- [ ] KEEP_FINAL_PACKAGE 폴더가 올바른지 확인",
        "- [ ] KEEP_CURRENT_RUN 폴더가 올바른지 확인",
        "- [ ] REVIEW_OLD_DIAGNOSTIC 항목 중 보관 필요한 것 있는지 확인",
        "- [ ] REVIEW_DUPLICATE_NETWORK 항목 중 현재 사용 중인 것 있는지 확인",
        "- [ ] REVIEW_TEMP_LOGS 항목 중 보관 필요한 것 있는지 확인",
        "",
        "> 실제 삭제는 이 계획서를 검토한 후 별도 수동 절차로 수행하십시오.",
        "> 이 스크립트는 아무것도 삭제하지 않습니다.",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 메인 빌더
# ---------------------------------------------------------------------------

def build_cleanup_plan(
    result_root: Path,
    output_csv: Path,
    keep_run_dir: Path | None = None,
    keep_final_package: Path | None = None,
) -> pd.DataFrame:
    rows = _scan(result_root, keep_run_dir, keep_final_package)
    df = (
        pd.DataFrame(rows)
        if rows
        else pd.DataFrame(columns=[
            "path", "type", "size_bytes", "size_mb",
            "category", "reason", "suggested_action",
        ])
    )
    df = df.sort_values("size_mb", ascending=False).reset_index(drop=True)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_csv, index=False, encoding="utf-8-sig")
    return df


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="result 디렉터리 정리 계획 생성기 (dry-run only — 삭제하지 않음)"
    )
    parser.add_argument("--result_root",       required=True, help="스캔할 result 디렉터리")
    parser.add_argument("--output_csv",        required=True, help="cleanup_plan.csv 출력 경로")
    parser.add_argument("--keep_run_dir",      default=None,  help="유지할 실행 결과 디렉터리")
    parser.add_argument("--keep_final_package",default=None,  help="유지할 최종 패키지 디렉터리")
    args = parser.parse_args()

    result_root        = Path(args.result_root)
    output_csv         = Path(args.output_csv)
    keep_run_dir       = Path(args.keep_run_dir)       if args.keep_run_dir       else None
    keep_final_package = Path(args.keep_final_package) if args.keep_final_package else None

    df = build_cleanup_plan(result_root, output_csv, keep_run_dir, keep_final_package)

    output_md = output_csv.with_suffix(".md")
    output_md.write_text(_build_md(df, result_root), encoding="utf-8")

    print(f"cleanup_plan_csv: {output_csv}")
    print(f"cleanup_plan_md:  {output_md}")
    print(f"총 항목 수: {len(df)}")
    print()

    for cat in sorted(df["category"].unique()):
        subset = df[df["category"] == cat]
        total_mb = subset["size_mb"].sum()
        print(f"  {cat:<40s} {len(subset):3d}개  {total_mb:8.1f} MB")

    print()
    print("--- 상위 10개 대용량 항목 ---")
    for _, row in df.nlargest(10, "size_mb").iterrows():
        print(f"  {row['size_mb']:8.1f} MB  {row['category']:<35s}  {row['path']}")


if __name__ == "__main__":
    main()
