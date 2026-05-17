#!/usr/bin/env python3
"""TLS Warning Audit Script — Missing green/yellow phase 경고 분석.

SUMO 로그에서 Missing green phase / Missing yellow phase 경고를 파싱하고,
실험 대상 횡단보도 TLS ID와의 교차 여부를 판단한다.

Usage:
    python scripts/audit_tls_warnings.py \
        --log-dir result/active/real_30seed_runs_sampled10/logs/current_main_12 \
        --candidate-dir result/active/real_30seed_runs_sampled10/runs/current_main_12 \
        --output result/active/real_30seed_runs_sampled10/tls_warning_audit.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Any

WARNING_PATTERN = re.compile(
    r"Warning: Missing (\w+) phase in tlLogic '([^']+)', program '([^']+)' for tl-index (\d+)"
)


def parse_log_warnings(log_path: Path) -> list[dict[str, Any]]:
    """로그 파일에서 Missing green/yellow phase 경고를 파싱한다."""
    warnings: list[dict[str, Any]] = []
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return warnings
    for line in text.splitlines():
        m = WARNING_PATTERN.search(line)
        if m:
            warnings.append({
                "missing_type": m.group(1),        # green or yellow
                "tls_id": m.group(2),
                "program_id": m.group(3),
                "tl_index": int(m.group(4)),
                "log_file": str(log_path),
            })
    return warnings


def find_target_tls_ids(candidate_dir: Path) -> dict[str, set[str]]:
    """resolved CSV에서 실험 대상 TLS ID를 추출한다.

    Returns:
        {crosswalk_id: {tls_id, tls_id_used, ...}}
    """
    result: dict[str, set[str]] = {}
    for csv_path in sorted(candidate_dir.rglob("sampled10_candidates_resolved.csv")):
        try:
            with open(csv_path, encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cid = row.get("crosswalk_id", "").strip()
                    tls_ids = set()
                    for key in ("tls_id", "tls_id_used", "nearest_junction_id"):
                        val = row.get(key, "").strip()
                        if val:
                            tls_ids.add(val)
                    if cid and tls_ids:
                        result.setdefault(cid, set()).update(tls_ids)
        except Exception:
            continue
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="TLS Warning Audit")
    parser.add_argument("--log-dir", required=True, help="SUMO 로그 디렉토리")
    parser.add_argument("--candidate-dir", required=True, help="실험 결과 디렉토리 (sampled10_candidates_resolved.csv 포함)")
    parser.add_argument("--output", required=True, help="출력 CSV 경로")
    parser.add_argument("--net-file", default=None, help="net.xml 경로 (선택, 추가 진단 용)")
    args = parser.parse_args()

    log_dir = Path(args.log_dir)
    candidate_dir = Path(args.candidate_dir)
    output_path = Path(args.output)

    # 1. 로그 파싱
    all_warnings: list[dict[str, Any]] = []
    for log_file in sorted(log_dir.rglob("*.log")):
        all_warnings.extend(parse_log_warnings(log_file))

    # 2. 실험 대상 TLS ID 추출
    target_map = find_target_tls_ids(candidate_dir)
    all_target_tls = set()
    for tls_ids in target_map.values():
        all_target_tls.update(tls_ids)

    # 3. 교차 여부 판단 및 요약
    unique_warning_tls: dict[str, dict[str, Any]] = {}
    for w in all_warnings:
        key = f"{w['tls_id']}_{w['tl_index']}_{w['missing_type']}"
        if key not in unique_warning_tls:
            unique_warning_tls[key] = {
                "tls_id": w["tls_id"],
                "tl_index": w["tl_index"],
                "missing_type": w["missing_type"],
                "program_id": w["program_id"],
                "occurrence_count": 0,
                "log_files": set(),
                "overlaps_experiment_target": w["tls_id"] in all_target_tls,
                "affected_crosswalk_ids": [],
            }
        unique_warning_tls[key]["occurrence_count"] += 1
        unique_warning_tls[key]["log_files"].add(w["log_file"])

    # 교차하는 crosswalk_id 매핑
    for key, entry in unique_warning_tls.items():
        affected = []
        for cid, tls_ids in target_map.items():
            if entry["tls_id"] in tls_ids:
                affected.append(cid)
        entry["affected_crosswalk_ids"] = affected

    # 4. 요약 통계
    total_warnings = len(all_warnings)
    unique_tls_count = len(set(w["tls_id"] for w in all_warnings))
    overlap_count = sum(1 for e in unique_warning_tls.values() if e["overlaps_experiment_target"])
    green_count = sum(1 for w in all_warnings if w["missing_type"] == "green")
    yellow_count = sum(1 for w in all_warnings if w["missing_type"] == "yellow")

    print(f"=== TLS Warning Audit Summary ===")
    print(f"Total warning lines:            {total_warnings}")
    print(f"  Missing green phase:          {green_count}")
    print(f"  Missing yellow phase:         {yellow_count}")
    print(f"Unique TLS IDs with warnings:   {unique_tls_count}")
    print(f"Experiment target TLS IDs:      {len(all_target_tls)}")
    print(f"  Targets: {sorted(all_target_tls)}")
    print(f"Overlap (target TLS in warnings): {overlap_count}")
    if overlap_count == 0:
        print(f"\n✅ 실험 대상 TLS와 경고 TLS 사이에 교차 없음.")
        print(f"   → 경고는 네트워크 내 다른 (비실험) 교차로에서만 발생.")
    else:
        print(f"\n⚠️  {overlap_count}개의 실험 대상 TLS에서 경고 발생!")
        for e in unique_warning_tls.values():
            if e["overlaps_experiment_target"]:
                print(f"   TLS={e['tls_id']} | index={e['tl_index']} | type={e['missing_type']} | crosswalks={e['affected_crosswalk_ids']}")

    # 5. CSV 출력
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "tls_id", "tl_index", "missing_type", "program_id",
        "occurrence_count", "overlaps_experiment_target",
        "affected_crosswalk_ids", "log_file_count",
    ]
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for entry in sorted(unique_warning_tls.values(), key=lambda e: (not e["overlaps_experiment_target"], e["tls_id"])):
            writer.writerow({
                "tls_id": entry["tls_id"],
                "tl_index": entry["tl_index"],
                "missing_type": entry["missing_type"],
                "program_id": entry["program_id"],
                "occurrence_count": entry["occurrence_count"],
                "overlaps_experiment_target": entry["overlaps_experiment_target"],
                "affected_crosswalk_ids": "|".join(entry["affected_crosswalk_ids"]),
                "log_file_count": len(entry["log_files"]),
            })
    print(f"\nAudit CSV written: {output_path}")
    print(f"Rows: {len(unique_warning_tls)}")

    # 6. baseline vs smart 경고 대칭성 확인
    baseline_logs = sorted(log_dir.rglob("baseline/**/*.log"))
    smart_logs = sorted(log_dir.rglob("smart/**/*.log"))
    if baseline_logs and smart_logs:
        baseline_tls = set()
        for log_file in baseline_logs:
            for w in parse_log_warnings(log_file):
                baseline_tls.add(w["tls_id"])
        smart_tls = set()
        for log_file in smart_logs:
            for w in parse_log_warnings(log_file):
                smart_tls.add(w["tls_id"])
        symmetric = baseline_tls == smart_tls
        print(f"\nBaseline-Smart warning symmetry: {'✅ IDENTICAL' if symmetric else '⚠️ DIFFERENT'}")
        if not symmetric:
            only_baseline = baseline_tls - smart_tls
            only_smart = smart_tls - baseline_tls
            if only_baseline:
                print(f"  Only in baseline: {sorted(only_baseline)[:10]}")
            if only_smart:
                print(f"  Only in smart: {sorted(only_smart)[:10]}")
    else:
        print(f"\nBaseline/smart log count: baseline={len(baseline_logs)}, smart={len(smart_logs)}")

    sys.exit(0 if overlap_count == 0 else 1)


if __name__ == "__main__":
    main()
