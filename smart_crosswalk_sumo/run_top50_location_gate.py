#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
RESULT_DIR = BASE_DIR / "result"
DEFAULT_DISTANCE_SUMMARY = RESULT_DIR / "phase_next_top50_mapping_visual_audit_20260515_174626" / "03_top50_mapping_distance_summary.csv"
DEFAULT_READY5 = RESULT_DIR / "phase_next_top50_tiered_recovery_audit" / "ready5_seed1_smoke_verdict.csv"
DEFAULT_MIXED = RESULT_DIR / "phase_next_top50_mixed_phase_redesign" / "mixed_phase_redesign_promoted_pedestrian_only_candidates.csv"
DEFAULT_OUT_DIR = RESULT_DIR / "phase_next_top50_mapping_visual_audit_20260515_174626"


def _normalize_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return ""
    if text.endswith(".0"):
        raw = text[:-2]
        if raw.replace("-", "", 1).isdigit():
            return raw
    return text


def _canonical_id(value: Any) -> str:
    text = _normalize_id(value)
    if not text:
        return ""
    if text.startswith("NODE_") or text.startswith("LINK_"):
        return _canonical_id(text.split("_", 1)[1])
    digits = "".join(ch for ch in text if ch.isdigit())
    return str(int(digits)) if digits else text


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path)


def _pick_status_for_ready(sub: pd.DataFrame, distance_grade: str) -> tuple[str, str, str]:
    if distance_grade in {"OK", "GOOD"}:
        return (
            "FINAL_READY_FOR_30SEED",
            "run_30seed",
            "signal ready and location reliable",
        )
    if distance_grade == "REVIEW":
        return (
            "LOCATION_REVIEW_BEFORE_30SEED",
            "hold",
            "signal ready but location review needed",
        )
    if distance_grade == "BAD":
        return (
            "DO_NOT_RUN_30SEED_UNTIL_LOCATION_FIXED",
            "hold",
            "location too far from real point",
        )
    return (
        "DO_NOT_RUN_30SEED_UNTIL_LOCATION_FIXED",
        "hold",
        "location missing",
    )


def _pick_status_for_mixed(sub: pd.DataFrame, distance_grade: str) -> tuple[str, str, str]:
    if distance_grade in {"OK", "GOOD"}:
        return (
            "RUN_SEED1_SMOKE_NEXT",
            "run_seed1_smoke",
            "smoke ready and location reliable",
        )
    if distance_grade in {"REVIEW", "BAD"}:
        return (
            "LOCATION_REVIEW_BEFORE_SMOKE",
            "hold",
            "location review needed before smoke",
        )
    return (
        "LOCATION_REVIEW_BEFORE_SMOKE",
        "hold",
        "location missing",
    )


def _summarize(df: pd.DataFrame) -> dict[str, int]:
    counts = Counter(df["distance_grade"].astype(str).tolist())
    return {k: int(counts.get(k, 0)) for k in ["OK", "GOOD", "REVIEW", "BAD", "MISSING"]}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Top 50 location gate classification")
    parser.add_argument("--distance-summary", default=str(DEFAULT_DISTANCE_SUMMARY))
    parser.add_argument("--ready5", default=str(DEFAULT_READY5))
    parser.add_argument("--mixed", default=str(DEFAULT_MIXED))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUT_DIR))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    distance_df = _load_csv(Path(args.distance_summary)).copy()
    ready_df = _load_csv(Path(args.ready5)).copy()
    mixed_df = _load_csv(Path(args.mixed)).copy()

    distance_df["crosswalk_id"] = distance_df["crosswalk_id"].map(_normalize_id)
    distance_df["canonical_crosswalk_id"] = distance_df["canonical_crosswalk_id"].map(_normalize_id)
    ready_df["crosswalk_id"] = ready_df["crosswalk_id"].map(_normalize_id)
    mixed_df["crosswalk_id"] = mixed_df["crosswalk_id"].map(_normalize_id)

    ready_ids = set(ready_df.loc[ready_df["verdict"].astype(str) == "READY_FOR_30SEED", "crosswalk_id"].tolist())
    mixed_ids = set(mixed_df["crosswalk_id"].tolist())

    rows: list[dict[str, Any]] = []
    for _, row in distance_df.iterrows():
        cid = _normalize_id(row.get("crosswalk_id"))
        current_tier = _normalize_id(row.get("current_recovery_tier"))
        distance_grade = _normalize_id(row.get("distance_grade"))
        if cid in ready_ids:
            location_gate_status, signal_smoke_status, reason = _pick_status_for_ready(ready_df[ready_df["crosswalk_id"] == cid], distance_grade)
            recommended_action = "run_30seed" if location_gate_status == "FINAL_READY_FOR_30SEED" else "hold"
        elif cid in mixed_ids:
            location_gate_status, signal_smoke_status, reason = _pick_status_for_mixed(mixed_df[mixed_df["crosswalk_id"] == cid], distance_grade)
            recommended_action = "run_seed1_smoke" if location_gate_status == "RUN_SEED1_SMOKE_NEXT" else "hold"
        else:
            location_gate_status = "OUTSIDE_SIGNAL_SMOKE_PIPELINE"
            signal_smoke_status = "NOT_IN_READY5_OR_MIXED"
            reason = "not present in ready5 or mixed redesign promotion inputs"
            recommended_action = "hold"

        if distance_grade == "BAD" and cid in ready_ids:
            location_gate_status = "DO_NOT_RUN_30SEED_UNTIL_LOCATION_FIXED"
            signal_smoke_status = "READY_FOR_30SEED"
            reason = "ready candidate but location is BAD"
            recommended_action = "hold"

        rows.append(
            {
                "crosswalk_id": cid,
                "original_rank": int(row.get("original_rank") or 0),
                "current_recovery_tier": current_tier,
                "distance_grade": distance_grade,
                "mapping_distance_m": row.get("mapping_distance_m"),
                "sumo_position_source": _normalize_id(row.get("sumo_position_source")),
                "signal_smoke_status": signal_smoke_status,
                "location_gate_status": location_gate_status,
                "recommended_action": recommended_action,
                "reason": reason,
            }
        )

    out = pd.DataFrame(rows)
    order = ["OK", "GOOD", "REVIEW", "BAD", "MISSING"]
    out["distance_grade"] = pd.Categorical(out["distance_grade"], categories=order, ordered=True)
    out = out.sort_values(["original_rank", "crosswalk_id"], kind="stable").reset_index(drop=True)

    gate_path = out_dir / "top50_experiment_candidate_location_gate.csv"
    bad_path = out_dir / "top50_bad_location_review_targets.csv"
    md_path = out_dir / "top50_location_gate_summary.md"

    out.to_csv(gate_path, index=False)
    bad_df = out[out["distance_grade"] == "BAD"].copy()
    bad_df.to_csv(bad_path, index=False)

    counts = _summarize(out)
    final_ready = int((out["location_gate_status"] == "FINAL_READY_FOR_30SEED").sum())
    location_review = int((out["location_gate_status"].isin(["LOCATION_REVIEW_BEFORE_30SEED", "LOCATION_REVIEW_BEFORE_SMOKE"])).sum())
    seed1_next = int((out["location_gate_status"] == "RUN_SEED1_SMOKE_NEXT").sum())
    do_not_run = int((out["location_gate_status"] == "DO_NOT_RUN_30SEED_UNTIL_LOCATION_FIXED").sum())
    outside = int((out["location_gate_status"] == "OUTSIDE_SIGNAL_SMOKE_PIPELINE").sum())

    ready_gate = out[out["location_gate_status"] == "FINAL_READY_FOR_30SEED"]["crosswalk_id"].tolist()
    mixed_gate = out[out["location_gate_status"] == "RUN_SEED1_SMOKE_NEXT"]["crosswalk_id"].tolist()
    bad_gate = out[out["distance_grade"] == "BAD"]["crosswalk_id"].tolist()

    md = "\n".join(
        [
            "# Top 50 Location Gate Summary",
            "",
            "## Counts",
            f"- total: {len(out)}",
            f"- OK: {counts['OK']}",
            f"- GOOD: {counts['GOOD']}",
            f"- REVIEW: {counts['REVIEW']}",
            f"- BAD: {counts['BAD']}",
            f"- MISSING: {counts['MISSING']}",
            "",
            "## Gate Results",
            f"- FINAL_READY_FOR_30SEED: {final_ready}",
            f"- RUN_SEED1_SMOKE_NEXT: {seed1_next}",
            f"- LOCATION_REVIEW_BEFORE_30SEED / LOCATION_REVIEW_BEFORE_SMOKE: {location_review}",
            f"- DO_NOT_RUN_30SEED_UNTIL_LOCATION_FIXED: {do_not_run}",
            f"- OUTSIDE_SIGNAL_SMOKE_PIPELINE: {outside}",
            "",
            "## Ready For 30 Seed",
            "- " + (", ".join(ready_gate) if ready_gate else "none"),
            "",
            "## Mixed Smoke Next",
            "- " + (", ".join(mixed_gate) if mixed_gate else "none"),
            "",
            "## Bad Location Review Targets",
            "- " + (", ".join(bad_gate) if bad_gate else "none"),
            "",
            "## Summary Sentence",
            "- Location gate keeps READY_FOR_30SEED only when distance_grade is OK/GOOD, sends REVIEW/BAD to review, and preserves mixed redesign smoke priority only for OK/GOOD rows.",
        ]
    ) + "\n"
    md_path.write_text(md, encoding="utf-8")

    print(f"gate_csv={gate_path}")
    print(f"bad_csv={bad_path}")
    print(f"summary_md={md_path}")
    print(f"final_ready={final_ready}")
    print(f"seed1_next={seed1_next}")
    print(f"location_review={location_review}")
    print(f"do_not_run={do_not_run}")
    print(f"outside={outside}")


if __name__ == "__main__":
    main()
