#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
SOURCE_CSV = BASE_DIR / "smart_crosswalk_sumo" / "data" / "crosswalk_stepwise_result_50m.csv"
DEFAULT_OUTPUT_HTML = BASE_DIR / "result" / "active" / "stepwise_top50_crosswalks_map.html"
DEFAULT_OUTPUT_CSV = BASE_DIR / "result" / "active" / "stepwise_top50_crosswalks_used.csv"

GRADE_PRIORITY = [
    "crosswalk_grade",
    "grade",
    "risk_grade",
    "install_grade",
    "eligibility_grade",
    "candidate_grade",
    "tier",
    "eligibility_tier",
    "phase6_status_label",
]

DEFAULT_COLOR = "gray"


def _tokenize_grade(text: str) -> set[str]:
    tokens = re.split(r"[^A-Z0-9]+", text.upper())
    return {tok for tok in tokens if tok}


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"missing csv: {path}")
    return pd.read_csv(path)


def _require_columns(df: pd.DataFrame, columns: list[str]) -> None:
    missing = [col for col in columns if col not in df.columns]
    if missing:
        raise ValueError(f"missing columns: {missing}")


def _pick_grade_column(df: pd.DataFrame) -> str:
    for col in GRADE_PRIORITY:
        if col in df.columns:
            return col
    raise ValueError(f"no grade column found. available columns: {list(df.columns)}")


def _grade_to_color(value: Any) -> str:
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return DEFAULT_COLOR
    upper = text.upper()
    tokens = _tokenize_grade(upper)

    if "FAIL" in tokens or "BAD" in tokens or "INVALID" in tokens or "NOT_READY" in tokens or "LOCATION_BAD" in tokens:
        return "red"

    if "FINAL_READY" in upper or "GOOD" in tokens or "HIGH" in tokens or (tokens == {"A"}) or (tokens == {"GRADE", "A"}):
        return "green"
    if "MEDIUM" in tokens or "PARTIAL" in tokens or tokens == {"B"} or tokens == {"GRADE", "B"}:
        return "orange"
    if "LOW" in tokens or "RECOVERY" in tokens or "GENERATED" in tokens or tokens == {"C"} or tokens == {"GRADE", "C"}:
        return "blue"
    return DEFAULT_COLOR


def _grade_sort_key(value: Any) -> str:
    text = str(value).strip()
    return text.lower()


def _build_popup(row: dict[str, Any]) -> str:
    return (
        '<table class="popup-table">'
        f"<tr><th>top50_rank</th><td>{html.escape(str(row['top50_rank']))}</td></tr>"
        f"<tr><th>crosswalk_id</th><td>{html.escape(str(row['crosswalk_id']))}</td></tr>"
        f"<tr><th>risk_score</th><td>{html.escape(str(row['risk_score']))}</td></tr>"
        f"<tr><th>dong_name</th><td>{html.escape(str(row.get('dong_name', '')))}</td></tr>"
        f"<tr><th>lon</th><td>{html.escape(str(row['lon']))}</td></tr>"
        f"<tr><th>lat</th><td>{html.escape(str(row['lat']))}</td></tr>"
        f"<tr><th>grade_column</th><td>{html.escape(str(row['grade_column']))}</td></tr>"
        f"<tr><th>grade_value</th><td>{html.escape(str(row['grade_value']))}</td></tr>"
        f"<tr><th>marker_color</th><td>{html.escape(str(row['marker_color']))}</td></tr>"
        "</table>"
    )


def _build_html(records: list[dict[str, Any]], source_csv: Path, grade_column: str, created_at: str) -> str:
    if records:
        center_lat = sum(float(r["lat"]) for r in records) / len(records)
        center_lon = sum(float(r["lon"]) for r in records) / len(records)
    else:
        center_lat, center_lon = 37.56, 126.99

    payload = json.dumps([{**r, "popup_html": _build_popup(r)} for r in records], ensure_ascii=False)
    legend_items = []
    seen: set[tuple[str, str]] = set()
    for r in records:
        key = (str(r["grade_value"]), str(r["marker_color"]))
        if key in seen:
            continue
        seen.add(key)
        legend_items.append(
            f'<span><i class="swatch" style="background:{html.escape(str(r["marker_color"]))}"></i>'
            f'{html.escape(str(r["grade_value"]))} <em>({html.escape(str(r["marker_color"]))})</em></span>'
        )
    legend_html = "".join(legend_items)

    table_rows = []
    for r in records:
        table_rows.append(
            "<tr>"
            f"<td>{html.escape(str(r['top50_rank']))}</td>"
            f"<td>{html.escape(str(r['crosswalk_id']))}</td>"
            f"<td>{html.escape(str(r['risk_score']))}</td>"
            f"<td>{html.escape(str(r.get('dong_name', '')))}</td>"
            f"<td>{html.escape(str(r['lon']))}</td>"
            f"<td>{html.escape(str(r['lat']))}</td>"
            f"<td>{html.escape(str(r['grade_column']))}</td>"
            f"<td>{html.escape(str(r['grade_value']))}</td>"
            f"<td>{html.escape(str(r['marker_color']))}</td>"
            "</tr>"
        )

    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Stepwise Top 50 Crosswalks by Grade</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>
    html, body {{ height: 100%; margin: 0; background: #0b1220; color: #e8eef7; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    #map {{ height: 72vh; width: 100%; }}
    .panel {{ padding: 14px 16px 16px; border-top: 1px solid #223047; background: linear-gradient(180deg, #10192c 0%, #0b1220 100%); }}
    .title {{ font-size: 18px; font-weight: 700; margin: 0 0 6px; }}
    .meta {{ font-size: 12px; color: #aab8cc; line-height: 1.5; margin-bottom: 12px; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 10px 14px; font-size: 12px; color: #c7d4e6; margin-bottom: 12px; }}
    .legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
    .legend em {{ font-style: normal; color: #8ea0ba; }}
    .swatch {{ width: 12px; height: 12px; border-radius: 999px; display: inline-block; border: 1px solid rgba(255,255,255,.35); }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; min-width: 1100px; border-collapse: collapse; background: #0e1628; }}
    th, td {{ border: 1px solid #223047; padding: 6px 8px; font-size: 12px; text-align: left; }}
    th {{ position: sticky; top: 0; background: #16213a; }}
    tr:nth-child(even) td {{ background: #10192c; }}
    .leaflet-popup-content-wrapper, .leaflet-popup-tip {{ background: #101622; color: #e4ebf5; }}
    .popup-table {{ border-collapse: collapse; }}
    .popup-table th, .popup-table td {{ border: 1px solid #24304a; padding: 4px 6px; font-size: 12px; }}
    .popup-table th {{ background: #162238; }}
  </style>
</head>
<body>
  <div id="map"></div>
  <div class="panel">
    <div class="title">Stepwise Top 50 Crosswalks by Grade</div>
    <div class="meta">
      total=50<br/>
      source_csv: {html.escape(str(source_csv))}<br/>
      grade_column: {html.escape(grade_column)}<br/>
      generated_at: {html.escape(created_at)}
    </div>
    <div class="legend">{legend_html}</div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>top50_rank</th><th>crosswalk_id</th><th>risk_score</th><th>dong_name</th>
            <th>lon</th><th>lat</th><th>grade_column</th><th>grade_value</th><th>marker_color</th>
          </tr>
        </thead>
        <tbody>{''.join(table_rows)}</tbody>
      </table>
    </div>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const data = {payload};
    const map = L.map('map', {{ preferCanvas: true }}).setView([{center_lat}, {center_lon}], 14);
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap'
    }}).addTo(map);

    const bounds = [];
    for (const r of data) {{
      const marker = L.circleMarker([r.lat, r.lon], {{
        radius: 8,
        color: r.marker_color,
        weight: 2,
        fillColor: r.marker_color,
        fillOpacity: 0.86
      }}).addTo(map);
      marker.bindPopup(r.popup_html);
      bounds.push([r.lat, r.lon]);
    }}
    if (bounds.length) {{
      map.fitBounds(bounds, {{ padding: [30, 30] }});
    }}
  </script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-csv", default=str(SOURCE_CSV))
    parser.add_argument("--output-html", default=str(DEFAULT_OUTPUT_HTML))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    args = parser.parse_args()

    source_csv = Path(args.source_csv)
    expected_source = SOURCE_CSV
    assert source_csv == expected_source, f"source_csv must be {expected_source}"

    df = _load_csv(source_csv)
    _require_columns(df, ["crosswalk_id", "risk_score", "lon", "lat"])

    grade_column = _pick_grade_column(df)

    work = df.copy()
    work["risk_score"] = pd.to_numeric(work["risk_score"], errors="raise")
    work["crosswalk_id"] = work["crosswalk_id"].astype(str)
    work["lon"] = pd.to_numeric(work["lon"], errors="coerce")
    work["lat"] = pd.to_numeric(work["lat"], errors="coerce")

    work = work.sort_values(
        by=["risk_score", "crosswalk_id"],
        ascending=[False, True],
        kind="mergesort",
    ).head(50).reset_index(drop=True)

    assert len(work) == 50, f"expected 50 rows, got {len(work)}"
    assert work["risk_score"].tolist() == sorted(work["risk_score"].tolist(), reverse=True), "risk_score not descending"
    assert work["lon"].notna().all() and work["lat"].notna().all(), "lon/lat has missing values"

    work.insert(0, "top50_rank", range(1, 51))
    assert work["top50_rank"].tolist() == list(range(1, 51)), "top50_rank must be 1..50"

    work["grade_column"] = grade_column
    work["grade_value"] = work[grade_column].astype(str)
    work["marker_color"] = work["grade_value"].map(_grade_to_color)

    used_cols = [
        "top50_rank",
        "crosswalk_id",
        "risk_score",
        "dong_name",
        "lon",
        "lat",
        "grade_column",
        "grade_value",
        "marker_color",
    ]
    for col in ["dong_name"]:
        if col not in work.columns:
            work[col] = ""

    used = work[used_cols].copy()
    assert all(col in used.columns for col in ["grade_column", "grade_value", "marker_color"]), "used.csv missing grade columns"

    records: list[dict[str, Any]] = []
    for _, row in used.iterrows():
        records.append(
            {
                "top50_rank": int(row["top50_rank"]),
                "crosswalk_id": str(row["crosswalk_id"]),
                "risk_score": float(row["risk_score"]),
                "dong_name": str(row.get("dong_name", "")),
                "lon": float(row["lon"]),
                "lat": float(row["lat"]),
                "grade_column": str(row["grade_column"]),
                "grade_value": str(row["grade_value"]),
                "marker_color": str(row["marker_color"]),
            }
        )

    assert len(records) == 50, f"expected 50 records, got {len(records)}"
    assert not any("NOT_IN_34_MAPPED_SET" in str(v) for v in used.astype(str).to_numpy().flatten()), "unexpected NOT_IN_34_MAPPED_SET in used.csv"

    out_html = Path(args.output_html)
    out_csv = Path(args.output_csv)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html_doc = _build_html(records, source_csv, grade_column, created_at)
    assert "NOT_IN_34_MAPPED_SET" not in html_doc, "unexpected NOT_IN_34_MAPPED_SET in HTML"

    used.to_csv(out_csv, index=False)
    out_html.write_text(html_doc, encoding="utf-8")

    print(f"source_csv: {source_csv}")
    print(f"grade_column: {grade_column}")
    print(f"output_html: {out_html}")
    print(f"output_csv: {out_csv}")
    print(f"rows: {len(used)}")
    print("marker_color counts:")
    for color, count in used["marker_color"].value_counts().sort_index().items():
        print(f"  {color}: {count}")
    print("grade_value counts:")
    grade_counts = used["grade_value"].value_counts().to_dict()
    for grade_value in sorted(grade_counts, key=_grade_sort_key):
        count = grade_counts[grade_value]
        print(f"  {grade_value}: {count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
