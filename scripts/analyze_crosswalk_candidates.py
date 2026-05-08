#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--candidates_csv", required=True)
    p.add_argument("--net_file", required=True)
    p.add_argument("--output_dir", required=True)
    return p.parse_args()


def parse_shape(shape: str) -> list[tuple[float, float]]:
    pts = []
    for tok in (shape or "").split():
        sp = tok.split(",")
        if len(sp) < 2:
            continue
        try:
            pts.append((float(sp[0]), float(sp[1])))
        except ValueError:
            pass
    return pts


def center_from_shape(shape: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not shape:
        return None
    xs = [p[0] for p in shape]
    ys = [p[1] for p in shape]
    return (float(np.mean(xs)), float(np.mean(ys)))


def read_net_data(net_file: Path) -> dict[str, Any]:
    root = ET.parse(net_file).getroot()
    loc = root.find("location")
    if loc is None:
        raise RuntimeError("location 없음")
    conv = [float(x) for x in loc.attrib["convBoundary"].split(",")]
    orig = [float(x) for x in loc.attrib["origBoundary"].split(",")]

    edge_function = {}
    crossing_rows = []
    crossing_ids = set()
    for e in root.findall("edge"):
        eid = e.attrib.get("id", "")
        fn = e.attrib.get("function", "normal")
        edge_function[eid] = fn
        if fn != "crossing":
            continue
        lane = e.find("lane")
        shape = parse_shape(lane.attrib.get("shape", "") if lane is not None else "")
        c = center_from_shape(shape)
        if c is None:
            continue
        crossing_ids.add(eid)
        crossing_rows.append(
            {
                "crossing_edge": eid,
                "junction": e.attrib.get("from", ""),
                "cx": c[0],
                "cy": c[1],
            }
        )

    tls_ids = set([tl.attrib.get("id", "") for tl in root.findall("tlLogic")])

    ped_links_by_crossing = defaultdict(set)
    crossing_conn_count = 0
    unique_tls_links = set()
    for c in root.findall("connection"):
        fr = c.attrib.get("from", "")
        to = c.attrib.get("to", "")
        if fr in crossing_ids or to in crossing_ids:
            crossing_conn_count += 1
            key_edge = fr if fr in crossing_ids else to
            li = c.attrib.get("linkIndex")
            if li is not None:
                ped_links_by_crossing[key_edge].add(int(li))
            tl = c.attrib.get("tl")
            if tl and li is not None:
                unique_tls_links.add((tl, li))

    return {
        "conv": conv,
        "orig": orig,
        "crossings": pd.DataFrame(crossing_rows),
        "tls_ids": tls_ids,
        "ped_links_by_crossing": {k: sorted(v) for k, v in ped_links_by_crossing.items()},
        "crossing_conn_count": crossing_conn_count,
        "unique_tls_links": len(unique_tls_links),
        "sumo_crossing_count": len(crossing_rows),
    }


def lonlat_to_xy_linear(lon: np.ndarray, lat: np.ndarray, conv: list[float], orig: list[float]) -> tuple[np.ndarray, np.ndarray]:
    cxmin, cymin, cxmax, cymax = conv
    lomin, lamin, lomax, lamax = orig
    x = cxmin + (lon - lomin) * (cxmax - cxmin) / (lomax - lomin)
    y = cymin + (lat - lamin) * (cymax - cymin) / (lamax - lamin)
    return x, y


def main() -> None:
    args = parse_args()
    cands = pd.read_csv(args.candidates_csv)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    nd = read_net_data(Path(args.net_file))
    cross = nd["crossings"]
    if cross.empty:
        raise RuntimeError("crossing 없음")

    lon = cands["longitude"].to_numpy(float)
    lat = cands["latitude"].to_numpy(float)
    x, y = lonlat_to_xy_linear(lon, lat, nd["conv"], nd["orig"])

    cx = cross["cx"].to_numpy(float)
    cy = cross["cy"].to_numpy(float)

    # vectorized nearest crossing by centroid distance
    dx = x[:, None] - cx[None, :]
    dy = y[:, None] - cy[None, :]
    idx = np.argmin(dx * dx + dy * dy, axis=1)

    matched = cross.iloc[idx].reset_index(drop=True)
    raw = pd.DataFrame(
        {
            "crosswalk_id": cands["crosswalk_id"].astype(str).values,
            "candidate_lon": lon,
            "candidate_lat": lat,
            "matched_crossing_edge": matched["crossing_edge"].values,
            "matched_junction": matched["junction"].values,
            "matched_tls_id": [j if j in nd["tls_ids"] else "" for j in matched["junction"].values],
            "ped_link_indices": [
                "|".join(map(str, nd["ped_links_by_crossing"].get(e, [])))
                for e in matched["crossing_edge"].values
            ],
            "ped_link_count": [len(nd["ped_links_by_crossing"].get(e, [])) for e in matched["crossing_edge"].values],
            "matched_crossing_xy_x": matched["cx"].values,
            "matched_crossing_xy_y": matched["cy"].values,
        }
    )
    raw.to_csv(out_dir / "crosswalk_candidates_raw.csv", index=False)

    dedup = raw.drop_duplicates(subset=["matched_crossing_edge"], keep="first")
    dedup.to_csv(out_dir / "crosswalk_candidates_deduplicated.csv", index=False)

    dup = raw.groupby("matched_crossing_edge", as_index=False).agg(
        matched_junction=("matched_junction", "first"),
        matched_tls_id=("matched_tls_id", "first"),
        candidate_count=("crosswalk_id", "count"),
        crosswalk_ids=("crosswalk_id", lambda s: "|".join(sorted(set(s)))),
    )
    dup = dup[dup["candidate_count"] > 1].sort_values("candidate_count", ascending=False)
    dup.to_csv(out_dir / "crosswalk_candidate_duplicates.csv", index=False)

    unique_candidate = int(raw["crosswalk_id"].nunique())
    unique_crossing = int(raw["matched_crossing_edge"].nunique())
    unique_junction = int(raw["matched_junction"].nunique())
    unique_location = int(raw[["matched_crossing_xy_x", "matched_crossing_xy_y"]].round(3).drop_duplicates().shape[0])

    md = [
        "# Crosswalk Candidate Summary",
        "",
        "## 해석",
        "- `crosswalk_candidate_matching.csv`의 `23040`은 건수가 아니라 `crosswalk_id` 값(단일 후보 식별자)입니다.",
        "",
        "## 재집계 결과",
        f"- 설치 후보 횡단보도 수(candidate id): {unique_candidate}",
        f"- unique crossing(edge) 수: {unique_crossing}",
        f"- unique junction 수: {unique_junction}",
        f"- unique location 수(XY 반올림 0.001): {unique_location}",
        f"- SUMO crossing 객체 수(edge function='crossing'): {nd['sumo_crossing_count']}",
        f"- SUMO crossing 관련 connection 수: {nd['crossing_conn_count']}",
        f"- SUMO crossing 관련 unique tls-link 수: {nd['unique_tls_links']}",
        "",
        "## 단위 권고",
        "- 설치 후보지 분석 단위: crosswalk_id",
        "- SUMO 제어/신호 분석 단위: crossing edge + tls/link",
        "- 보고서에서 두 단위를 분리 표기할 것",
    ]
    (out_dir / "crosswalk_candidate_summary.md").write_text("\n".join(md), encoding="utf-8")

    print(
        json.dumps(
            {
                "installation_candidate_count": unique_candidate,
                "unique_matched_crossing_count": unique_crossing,
                "unique_matched_junction_count": unique_junction,
                "unique_matched_location_count": unique_location,
                "sumo_crossing_object_count": int(nd["sumo_crossing_count"]),
                "sumo_crossing_connection_count": int(nd["crossing_conn_count"]),
                "sumo_unique_tls_link_count": int(nd["unique_tls_links"]),
                "duplicate_group_count": int(len(dup)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
