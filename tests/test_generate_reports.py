from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import smart_crosswalk_sumo.generate_reports as generate_reports


def test_generate_all_reports_prefers_singular_simulation_result_and_writes_plural_compatibility(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output_dir = tmp_path / "run"
    figures_dir = tmp_path / "figures"
    output_dir.mkdir()

    source_df = pd.DataFrame(
        [
            {
                "crosswalk_id": "NODE_1",
                "scenario": "baseline",
                "seed": 1,
                "value": 123,
            }
        ]
    )
    source_df.to_csv(output_dir / "simulation_result.csv", index=False)

    captured: dict[str, pd.DataFrame] = {}

    def fake_build_simulation_summary(avg_df: pd.DataFrame, seed_df: pd.DataFrame) -> pd.DataFrame:
        captured["avg_df"] = avg_df.copy()
        captured["seed_df"] = seed_df.copy()
        return pd.DataFrame([{"crosswalk_id": "NODE_1", "scenario": "baseline"}])

    monkeypatch.setattr(generate_reports, "load_model_parameters", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(generate_reports, "build_simulation_summary", fake_build_simulation_summary)
    monkeypatch.setattr(
        generate_reports,
        "build_baseline_vs_smart_summary",
        lambda summary_df, metrics_exact: summary_df.assign(metrics_exact=metrics_exact),
    )
    monkeypatch.setattr(generate_reports, "write_methodology_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(generate_reports, "write_figures", lambda *args, **kwargs: None)
    monkeypatch.setattr(generate_reports, "write_required_outputs", lambda *args, **kwargs: None)

    generate_reports.generate_all_reports(
        output_dir=output_dir,
        figures_dir=figures_dir,
        candidates_csv=None,
        nets_dir=None,
    )

    assert captured["avg_df"].equals(source_df)
    assert captured["seed_df"].empty
    assert (output_dir / "csv" / "results" / "simulation_results.csv").is_file()
    assert (output_dir / "simulation_results.csv").is_file()
