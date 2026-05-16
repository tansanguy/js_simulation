#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_HTML = BASE_DIR / "result" / "active" / "mapped_50_crosswalks_map.html"
DEFAULT_OUTPUT_CSV = BASE_DIR / "result" / "active" / "mapped_50_crosswalks_used.csv"

PRIMARY_SOURCE = (
    BASE_DIR
    / "oldresult"
    / "archived_20260516_184054"
    / "phase_next_top50_max_installation_recovery_20260516_002620"
    / "top50_max_installation_master.csv"
)
STEPWISE_SOURCE = BASE_DIR / "oldresult" / "crosswalk_stepwise_result.csv"
TOP50_SOURCE = BASE_DIR / "result" / "active" / "pedestrian_assumption" / "top50_mapped_candidates_daytime_high_1p2.csv"


def _norm_id(value: Any) -> str:
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return ""
    if text.endswith(".0"):
        text = text[:-2]
    if text.startswith(("NODE_", "LINK_")):
        text = text.split("_", 1)[1]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits or text


def _safe_float(value: Any) -> float:
    try:
        if pd.isna(value):
            return math.nan
        return float(value)
    except Exception:
        return math.nan


def _pick_coord_columns(df: pd.DataFrame) -> tuple[str, str]:
    pairs = [
        ("real_lon", "real_lat"),
        ("longitude", "latitude"),
        ("lon", "lat"),
        ("current_sumo_lon", "current_sumo_lat"),
    ]
    for lon_col, lat_col in pairs:
        if lon_col in df.columns and lat_col in df.columns:
            return lon_col, lat_col
    raise ValueError("좌표 컬럼 못 찾음. longitude/latitude, lon/lat, real_lon/real_lat 중 필요")


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def _select_source_csv() -> Path:
    for path in [TOP50_SOURCE, PRIMARY_SOURCE, STEPWISE_SOURCE]:
        if path.exists():
            return path
    raise FileNotFoundError("34개 후보 CSV 못 찾음")


def _select_status_rows(df: pd.DataFrame) -> pd.DataFrame:
    if "crosswalk_id" not in df.columns:
        raise ValueError("source CSV 에 crosswalk_id 없음")
    work = df.copy()
    work["canonical_crosswalk_id"] = work["crosswalk_id"].map(_norm_id)
    work = work[work["canonical_crosswalk_id"] != ""].copy()
    if "original_rank" in work.columns:
        work["original_rank"] = pd.to_numeric(work["original_rank"], errors="coerce")
        work = work.sort_values(["original_rank", "canonical_crosswalk_id"], kind="stable")
    work = work.drop_duplicates("canonical_crosswalk_id", keep="first").reset_index(drop=True)
    return work


def _build_enrichment(stepwise: pd.DataFrame, top50: pd.DataFrame) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    if not stepwise.empty and "crosswalk_id" in stepwise.columns:
        s = stepwise.copy()
        s["canonical_crosswalk_id"] = s["crosswalk_id"].map(_norm_id)
        frames.append(s)
    if not top50.empty and "crosswalk_id" in top50.columns:
        t = top50.copy()
        t["canonical_crosswalk_id"] = t["crosswalk_id"].map(_norm_id)
        frames.append(t)
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True, sort=False)
    merged = merged.drop_duplicates("canonical_crosswalk_id", keep="first")
    return merged


def _build_status_lookup(df: pd.DataFrame) -> dict[str, str]:
    if df.empty or "crosswalk_id" not in df.columns:
        return {}
    lookup: dict[str, str] = {}
    for _, row in df.iterrows():
        cid = _norm_id(row.get("crosswalk_id"))
        if not cid:
            continue
        status = _pick_col(row, ["current_status", "status_label", "phase6_status_label", "final_verdict", "runtime_status"])
        if status:
            lookup[cid] = str(status)
    return lookup


def _pick_col(row: pd.Series, names: list[str]) -> Any:
    for name in names:
        if name in row.index and pd.notna(row.get(name)):
            return row.get(name)
    return ""


def _make_output_path(path: Path, stamp: str | None = None) -> Path:
    if not path.exists():
        return path
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    return path.with_name(f"{path.stem}_{stamp}{path.suffix}")


def _risk_color(score: float | None) -> str:
    if score is None or math.isnan(score):
        return "#8b949e"
    # low -> green, high -> red
    clamped = max(0.0, min(1.0, score))
    r = int(46 + (231 - 46) * clamped)
    g = int(204 + (76 - 204) * clamped)
    b = int(113 + (60 - 113) * clamped)
    return f"#{r:02x}{g:02x}{b:02x}"


def _risk_radius(score: float | None) -> float:
    if score is None or math.isnan(score):
        return 6.0
    clamped = max(0.0, min(1.0, score))
    return 6.0 + clamped * 6.0


def _build_html(records: list[dict[str, Any]], source_csv: Path, created_at: str) -> str:
    if records:
        center_lat = sum(float(r["latitude"]) for r in records) / len(records)
        center_lon = sum(float(r["longitude"]) for r in records) / len(records)
    else:
        center_lat, center_lon = 37.56, 126.99

    payload = json.dumps(records, ensure_ascii=False)
    body_rows = []
    for r in records:
        body_rows.append(
            "<tr>"
            f"<td>{html.escape(str(r.get('crosswalk_id', '')))}</td>"
            f"<td>{html.escape(str(r.get('rank', '')))}</td>"
            f"<td>{html.escape(str(r.get('risk_score', '')))}</td>"
            f"<td>{html.escape(str(r.get('dong_name', '')))}</td>"
            f"<td>{html.escape(str(r.get('status', '')))}</td>"
            "</tr>"
        )
    table_html = "".join(body_rows)
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Mapped 50 Crosswalks</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>
    html, body {{ height: 100%; margin: 0; background: #0b1220; color: #e5eefb; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    #map {{ height: 72vh; width: 100%; }}
    .panel {{ padding: 14px 16px 16px; border-top: 1px solid #223047; background: linear-gradient(180deg, #10192c 0%, #0b1220 100%); }}
    .title {{ font-size: 18px; font-weight: 700; margin: 0 0 6px; }}
    .meta {{ font-size: 12px; color: #aab8cc; line-height: 1.5; margin-bottom: 12px; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 10px 14px; font-size: 12px; color: #c7d4e6; margin-bottom: 12px; }}
    .legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
    .swatch {{ width: 12px; height: 12px; border-radius: 999px; display: inline-block; border: 1px solid rgba(255,255,255,.35); }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; min-width: 860px; border-collapse: collapse; background: #0e1628; }}
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
    <div class="title">Mapped 50 Crosswalks</div>
    <div class="meta">
      total: {len(records)}<br/>
      source_csv: {html.escape(str(source_csv))}<br/>
      generated_at: {html.escape(created_at)}
    </div>
    <div class="legend">
      <span><i class="swatch" style="background:#2ecc71"></i>low risk</span>
      <span><i class="swatch" style="background:#f1c40f"></i>mid risk</span>
      <span><i class="swatch" style="background:#e74c3c"></i>high risk</span>
      <span><i class="swatch" style="background:#8b949e"></i>missing risk</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>crosswalk_id</th><th>rank</th><th>risk_score</th><th>dong_name</th><th>status</th></tr></thead>
        <tbody>{table_html}</tbody>
      </table>
    </div>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const data = {payload};
    const map = L.map('map', {{ preferCanvas: true }}).setView([{center_lat}, {center_lon}], 14);
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{ maxZoom: 19, attribution: '&copy; OpenStreetMap' }}).addTo(map);

    function popupHtml(r) {{
      return `
        <table class="popup-table">
          <tr><th>crosswalk_id</th><td>${{r.crosswalk_id}}</td></tr>
          <tr><th>rank</th><td>${{r.rank}}</td></tr>
          <tr><th>risk_score</th><td>${{r.risk_score ?? ''}}</td></tr>
          <tr><th>dong_name</th><td>${{r.dong_name ?? ''}}</td></tr>
          <tr><th>status</th><td>${{r.status ?? ''}}</td></tr>
        </table>`;
    }}

    const bounds = [];
    for (const r of data) {{
      const marker = L.circleMarker([r.latitude, r.longitude], {{
        radius: r.radius,
        color: r.color,
        weight: 2,
        fillColor: r.color,
        fillOpacity: 0.82
      }}).addTo(map);
      marker.bindPopup(popupHtml(r));
      bounds.push([r.latitude, r.longitude]);
    }}
    if (bounds.length) {{
      map.fitBounds(bounds, {{ padding: [30, 30] }});
    }}
  </script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-html", default=str(DEFAULT_OUTPUT_HTML))
    parser.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    parser.add_argument("--source-csv", default="")
    args = parser.parse_args()

    source_csv = Path(args.source_csv) if args.source_csv else _select_source_csv()
    print(f"using source csv: {source_csv}")

    source = _load_csv(source_csv)
    if source.empty:
        raise FileNotFoundError(f"source csv empty or missing: {source_csv}")

    if len(source) != 50:
        print(f"warning: source rows {len(source)} not 50")

    stepwise = _load_csv(STEPWISE_SOURCE)
    top50 = _load_csv(TOP50_SOURCE)
    enrich = _build_enrichment(stepwise, top50)
    status_lookup = _build_status_lookup(_load_csv(PRIMARY_SOURCE))

    selected = source.copy()
    selected["canonical_crosswalk_id"] = selected["crosswalk_id"].map(_norm_id)
    selected = selected[selected["canonical_crosswalk_id"] != ""].copy()
    if "top50_rank" in selected.columns:
        selected["top50_rank"] = pd.to_numeric(selected["top50_rank"], errors="coerce")
    if "risk_score" in selected.columns:
        selected["risk_score"] = pd.to_numeric(selected["risk_score"], errors="coerce")
    selected = selected.drop_duplicates("canonical_crosswalk_id", keep="first").reset_index(drop=True)

    lon_col, lat_col = _pick_coord_columns(selected)
    print(f"detected coord columns: {lon_col}/{lat_col}")
    timestamp_suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
    if not enrich.empty and "canonical_crosswalk_id" in enrich.columns:
        selected = selected.merge(
            enrich.drop(columns=[c for c in ["crosswalk_id"] if c in enrich.columns], errors="ignore"),
            on="canonical_crosswalk_id",
            how="left",
            suffixes=("", "_enrich"),
        )

    if "canonical_crosswalk_id" not in selected.columns:
        selected["canonical_crosswalk_id"] = selected["crosswalk_id"].map(_norm_id)

    records: list[dict[str, Any]] = []
    skipped: list[str] = []
    for _, row in selected.iterrows():
        lon = _safe_float(_pick_col(row, [lon_col, "real_lon", "longitude", "lon", "current_sumo_lon"]))
        lat = _safe_float(_pick_col(row, [lat_col, "real_lat", "latitude", "lat", "current_sumo_lat"]))
        if math.isnan(lon) or math.isnan(lat):
            skipped.append(str(row.get("crosswalk_id", "")))
            continue
        risk = _safe_float(_pick_col(row, ["risk_score"]))
        if math.isnan(risk):
            risk = math.nan
        norm_risk = None if math.isnan(risk) else risk
        rank = _pick_col(row, ["original_rank", "top50_rank"])
        status = _pick_col(row, ["current_status", "status_label", "phase6_status_label", "final_verdict", "runtime_status"])
        if not status:
            status = status_lookup.get(_norm_id(row.get("crosswalk_id")), "NOT_IN_34_MAPPED_SET")
        records.append(
            {
                "crosswalk_id": str(row.get("crosswalk_id", "")),
                "rank": "" if pd.isna(rank) else int(float(rank)),
                "risk_score": "" if norm_risk is None else round(float(norm_risk), 6),
                "dong_name": str(_pick_col(row, ["dong_name", "admin_dong"])),
                "status": str(status),
                "longitude": lon,
                "latitude": lat,
                "color": _risk_color(norm_risk),
                "radius": _risk_radius(norm_risk),
                "source_csv": str(source_csv),
            }
        )

    if skipped:
        print(f"warning: skipped rows without coordinates: {len(skipped)} -> {', '.join(skipped)}")

    assert len(records) == 50, f"expected 50 visualized rows, got {len(records)}"

    out_html = _make_output_path(Path(args.output_html), timestamp_suffix)
    out_csv = _make_output_path(Path(args.output_csv), timestamp_suffix)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html_doc = _build_html(records, source_csv, created_at)
    pd.DataFrame(records).to_csv(out_csv, index=False)
    out_html.write_text(html_doc, encoding="utf-8")

    print(f"output html: {out_html}")
    print(f"output csv: {out_csv}")
    print(f"rows: {len(records)}")


if __name__ == "__main__":
    main()
