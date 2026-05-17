from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

from .output_schema import write_csv_utf8_sig


@dataclass(frozen=True)
class CsvOutputLayout:
    root: Path
    report: Path
    results: Path
    internal: Path


def resolve_csv_output_layout(output_dir: str | Path) -> CsvOutputLayout:
    root = Path(output_dir) / "csv"
    return CsvOutputLayout(
        root=root,
        report=root / "report",
        results=root / "results",
        internal=root / "internal",
    )


def ensure_csv_output_layout(output_dir: str | Path) -> CsvOutputLayout:
    layout = resolve_csv_output_layout(output_dir)
    layout.report.mkdir(parents=True, exist_ok=True)
    layout.results.mkdir(parents=True, exist_ok=True)
    layout.internal.mkdir(parents=True, exist_ok=True)
    return layout


def write_csv_bundle(
    df: pd.DataFrame,
    primary: str | Path,
    *,
    mirrors: Iterable[str | Path] = (),
    index: bool = False,
) -> None:
    primary_path = Path(primary)
    write_csv_utf8_sig(df, primary_path, index=index)
    for mirror in mirrors:
        mirror_path = Path(mirror)
        if mirror_path == primary_path:
            continue
        write_csv_utf8_sig(df, mirror_path, index=index)


def mirror_existing_csv_files(
    output_dir: str | Path,
    *,
    results_files: Iterable[str] = (),
    internal_files: Iterable[str] = (),
) -> CsvOutputLayout:
    output_path = Path(output_dir)
    layout = ensure_csv_output_layout(output_path)
    for name in results_files:
        source = output_path / name
        if source.exists():
            try:
                df = pd.read_csv(source, encoding="utf-8-sig")
                write_csv_utf8_sig(df, layout.results / name)
            except Exception:
                pass
    for name in internal_files:
        source = output_path / name
        if source.exists():
            try:
                df = pd.read_csv(source, encoding="utf-8-sig")
                write_csv_utf8_sig(df, layout.internal / name)
            except Exception:
                pass
    return layout
