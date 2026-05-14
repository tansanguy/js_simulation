#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import shlex
from pathlib import Path

import pandas as pd


def _parse_range(raw: str) -> list[int]:
    raw = str(raw).strip()
    if not raw:
        return []
    if "-" in raw and "," not in raw:
        start_s, end_s = raw.split("-", 1)
        start = int(start_s)
        end = int(end_s)
        if end < start:
            start, end = end, start
        return list(range(start, end + 1))
    out: list[int] = []
    for token in raw.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        out.append(int(token))
    return out


def _parse_float_list(raw: str) -> list[float]:
    out: list[float] = []
    for token in str(raw).replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        out.append(float(token))
    return out


def _project_root(raw: str | None) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def _candidate_scope_label(limit_crosswalk_ids: list[str] | None) -> str:
    if not limit_crosswalk_ids:
        return "all"
    return "limit_" + "__".join(limit_crosswalk_ids)


def _build_command(
    candidate_csv: Path,
    net_file: Path,
    scenario: str,
    seed: int,
    sim_duration: int,
    warmup: int,
    extension_sec: float,
    output_dir: Path,
    vehicle_hourly_volume: float,
    vehicle_arrival_process: str,
    impact_radii_m: str,
    include_global_scope: bool,
    limit_crosswalk_ids: list[str] | None,
) -> list[str]:
    cmd = [
        "python3",
        "smart_crosswalk_sumo/run_phase6_vehicle_flow.py",
        "--candidate-csv",
        str(candidate_csv),
        "--net-file",
        str(net_file),
        "--scenario",
        scenario,
        "--seed",
        str(seed),
        "--sim-duration",
        str(sim_duration),
        "--warmup",
        str(warmup),
        "--extension-sec",
        str(extension_sec),
        "--output-dir",
        str(output_dir),
        "--vehicle-demand-mode",
        "hourly",
        "--vehicle-hourly-volume",
        str(vehicle_hourly_volume),
        "--vehicle-arrival-process",
        vehicle_arrival_process,
        "--impact-radii-m",
        impact_radii_m,
    ]
    if include_global_scope:
        cmd.append("--include-global-scope")
    else:
        cmd.append("--no-include-global-scope")
    if limit_crosswalk_ids:
        cmd.append("--limit-crosswalk-ids")
        cmd.extend(limit_crosswalk_ids)
    return cmd


def _candidate_crosswalk_ids(candidate_csv: Path, limit_crosswalk_ids: list[str] | None) -> list[str]:
    if limit_crosswalk_ids:
        return list(dict.fromkeys([str(v).strip() for v in limit_crosswalk_ids if str(v).strip()]))
    df = pd.read_csv(candidate_csv)
    if "crosswalk_id" not in df.columns:
        raise ValueError("candidate csv missing crosswalk_id")
    return list(dict.fromkeys([str(v) for v in df["crosswalk_id"].astype(str).tolist()]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Phase 6 vehicle experiment run plan only.")
    parser.add_argument("--candidate-csv", required=True)
    parser.add_argument("--net-file", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--sim-duration", type=int, default=600)
    parser.add_argument("--warmup", type=int, default=0)
    parser.add_argument("--extension-sec", type=float, default=5.0)
    parser.add_argument("--seeds", default="1-30")
    parser.add_argument("--vehicle-hourly-volumes", default="300,600,900")
    parser.add_argument("--vehicle-arrival-process", choices=["deterministic", "poisson"], default="deterministic")
    parser.add_argument("--impact-radii-m", default="100,300,500,1000")
    parser.add_argument("--include-global-scope", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--output-layout", choices=["grouped", "by-crosswalk"], default="grouped")
    parser.add_argument("--limit-crosswalk-ids", nargs="*")
    parser.add_argument("--experiment-name", default="phase6_vehicle_experiment_draft")
    args = parser.parse_args()

    candidate_csv = Path(args.candidate_csv).expanduser().resolve()
    net_file = Path(args.net_file).expanduser().resolve()
    output_root = Path(args.output_root).expanduser().resolve()
    experiment_dir = output_root / args.experiment_name
    experiment_dir.mkdir(parents=True, exist_ok=True)
    project_root = _project_root(args.project_root)

    seeds = _parse_range(args.seeds)
    hourly_volumes = _parse_float_list(args.vehicle_hourly_volumes)
    limit_crosswalk_ids = [str(v).strip() for v in args.limit_crosswalk_ids] if args.limit_crosswalk_ids else None
    candidate_ids = _candidate_crosswalk_ids(candidate_csv, limit_crosswalk_ids)
    candidate_scope = "batch" if args.output_layout == "grouped" else "single_crosswalk"
    run_unit = "batch" if args.output_layout == "grouped" else "crosswalk"
    commands_count = 0
    rows: list[dict[str, object]] = []
    command_lines: list[str] = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"cd {project_root}",
        f'export PYTHONPATH="{project_root}:${{PYTHONPATH:-}}"',
        "",
    ]

    if args.output_layout == "grouped":
        crosswalk_plan = [("ALL", None)]
    else:
        crosswalk_plan = [(cid, [cid]) for cid in candidate_ids]

    for volume in hourly_volumes:
        for crosswalk_id, crosswalk_limit in crosswalk_plan:
            for seed in seeds:
                for scenario in ("baseline", "smart"):
                    if args.output_layout == "grouped":
                        run_output_dir = experiment_dir / f"volume_{int(volume)}" / f"seed_{seed:02d}" / scenario
                    else:
                        run_output_dir = (
                            experiment_dir
                            / f"crosswalk_{crosswalk_id}"
                            / f"volume_{int(volume)}"
                            / f"seed_{seed:02d}"
                            / scenario
                        )
                    cmd = _build_command(
                        candidate_csv,
                        net_file,
                        scenario,
                        seed,
                        int(args.sim_duration),
                        int(args.warmup),
                        float(args.extension_sec),
                        run_output_dir,
                        float(volume),
                        args.vehicle_arrival_process,
                        str(args.impact_radii_m),
                        bool(args.include_global_scope),
                        crosswalk_limit,
                    )
                    command = shlex.join(cmd)
                    command_lines.append(command)
                    commands_count += 1
                    rows.append(
                        {
                            "experiment_name": args.experiment_name,
                            "output_layout": args.output_layout,
                            "crosswalk_id": crosswalk_id,
                            "scenario": scenario,
                            "seed": seed,
                            "vehicle_hourly_volume": float(volume),
                            "sim_duration": int(args.sim_duration),
                            "warmup": int(args.warmup),
                            "extension_sec": float(args.extension_sec),
                            "candidate_scope": candidate_scope,
                            "run_unit": run_unit,
                            "output_dir": str(run_output_dir),
                            "command": command,
                        }
                    )

    manifest_csv = experiment_dir / "phase6_vehicle_experiment_manifest.csv"
    with manifest_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "experiment_name",
                "output_layout",
                "crosswalk_id",
                "scenario",
                "seed",
                "vehicle_hourly_volume",
                "sim_duration",
                "warmup",
                "extension_sec",
                "candidate_scope",
                "run_unit",
                "output_dir",
                "command",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    commands_sh = experiment_dir / "phase6_vehicle_experiment_commands.sh"
    commands_sh.write_text("\n".join(command_lines) + "\n", encoding="utf-8")
    try:
        commands_sh.chmod(0o755)
    except Exception:
        pass

    plan_md = experiment_dir / "phase6_vehicle_experiment_plan.md"
    total_runs = len(rows)
    grouped_runs = len(hourly_volumes) * len(seeds) * 2
    by_crosswalk_runs = len(hourly_volumes) * len(seeds) * 2 * len(candidate_ids)
    plan_md.write_text(
        "\n".join(
            [
                "# Phase 6 Vehicle Experiment Plan",
                "",
                f"- experiment_name: {args.experiment_name}",
                f"- candidate_csv: {candidate_csv}",
                f"- net_file: {net_file}",
                f"- project_root: {project_root}",
                f"- seeds: {len(seeds)} ({seeds[0]} to {seeds[-1]})" if seeds else "- seeds: 0",
                f"- hourly_volumes: {hourly_volumes}",
                f"- scenarios: baseline, smart",
                f"- impact_radii_m: {args.impact_radii_m}",
                f"- include_global_scope: {bool(args.include_global_scope)}",
                f"- output_layout: {args.output_layout}",
                f"- candidate_scope: {candidate_scope}",
                f"- run_unit: {run_unit}",
                f"- candidate_ids: {candidate_ids}",
                f"- grouped_runs: {grouped_runs}",
                f"- by_crosswalk_runs: {by_crosswalk_runs}",
                f"- planned_commands: {commands_count}",
                f"- total_manifest_rows: {total_runs}",
                "",
                "This plan only writes commands. It does not run SUMO.",
                "grouped layout keeps one run per seed/scenario/volume.",
                "by-crosswalk layout splits runs by crosswalk and increases the run count.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"manifest_csv: {manifest_csv}")
    print(f"commands_sh: {commands_sh}")
    print(f"plan_md: {plan_md}")
    print(f"planned_commands: {commands_count}")


if __name__ == "__main__":
    main()
