#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
from PIL import Image
from pyproj import CRS, Transformer

TILE_SIZE = 256
OSM_TILE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
UA = {"User-Agent": "sumo-network-overlay-validator/1.0"}


def lonlat_to_global_px(lon: float, lat: float, z: int) -> tuple[float, float]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    x = (lon + 180.0) / 360.0
    sin_lat = math.sin(math.radians(lat))
    y = 0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)
    scale = TILE_SIZE * (2**z)
    return x * scale, y * scale


def global_px_to_lonlat(px: float, py: float, z: int) -> tuple[float, float]:
    scale = TILE_SIZE * (2**z)
    x = px / scale
    y = py / scale
    lon = x * 360.0 - 180.0
    n = math.pi - 2.0 * math.pi * y
    lat = math.degrees(math.atan(math.sinh(n)))
    return lon, lat


def best_zoom(min_lon: float, min_lat: float, max_lon: float, max_lat: float, width: int, height: int) -> int:
    for z in range(19, 8, -1):
        x1, y1 = lonlat_to_global_px(min_lon, max_lat, z)
        x2, y2 = lonlat_to_global_px(max_lon, min_lat, z)
        if (x2 - x1) <= width * 0.9 and (y2 - y1) <= height * 0.9:
            return z
    return 12


def fetch_tile(z: int, x: int, y: int) -> Image.Image:
    url = OSM_TILE.format(z=z, x=x, y=y)
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return Image.open(BytesIO(r.content)).convert("RGB")


def build_basemap(min_lon: float, min_lat: float, max_lon: float, max_lat: float, width: int, height: int) -> tuple[Image.Image, dict]:
    z = best_zoom(min_lon, min_lat, max_lon, max_lat, width, height)
    gminx, gmaxy = lonlat_to_global_px(min_lon, max_lat, z)
    gmaxx, gminy = lonlat_to_global_px(max_lon, min_lat, z)

    tx0, ty0 = int(gminx // TILE_SIZE), int(gmaxy // TILE_SIZE)
    tx1, ty1 = int(gmaxx // TILE_SIZE), int(gminy // TILE_SIZE)

    canvas = Image.new("RGB", ((tx1 - tx0 + 1) * TILE_SIZE, (ty1 - ty0 + 1) * TILE_SIZE), (245, 245, 245))
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            try:
                tile = fetch_tile(z, tx, ty)
                canvas.paste(tile, ((tx - tx0) * TILE_SIZE, (ty - ty0) * TILE_SIZE))
            except Exception:
                pass

    crop_l = int(gminx - tx0 * TILE_SIZE)
    crop_t = int(gmaxy - ty0 * TILE_SIZE)
    crop_r = int(gmaxx - tx0 * TILE_SIZE)
    crop_b = int(gminy - ty0 * TILE_SIZE)
    cropped = canvas.crop((crop_l, crop_t, crop_r, crop_b)).resize((width, height), Image.Resampling.BILINEAR)
    meta = {
        "zoom": z,
        "bbox": (min_lon, min_lat, max_lon, max_lat),
    }
    return cropped, meta


def parse_shape(shape_text: str) -> list[tuple[float, float]]:
    pts = []
    for tok in (shape_text or "").split():
        sp = tok.split(",")
        if len(sp) < 2:
            continue
        try:
            pts.append((float(sp[0]), float(sp[1])))
        except ValueError:
            continue
    return pts


def parse_proj_transformer(proj_param: str | None) -> Transformer | None:
    if not proj_param or proj_param == "!":
        return None
    try:
        src = CRS.from_proj4(proj_param)
        dst = CRS.from_epsg(4326)
        return Transformer.from_crs(src, dst, always_xy=True)
    except Exception:
        return None


def xy_to_lonlat(
    x: float,
    y: float,
    conv: tuple[float, float, float, float],
    orig: tuple[float, float, float, float],
    transformer: Transformer | None,
    net_offset: tuple[float, float] | None,
) -> tuple[float, float]:
    if transformer is not None:
        if net_offset is not None:
            # SUMO local coords are shifted by netOffset: local = projected + netOffset
            # so projected = local - netOffset.
            x = x - net_offset[0]
            y = y - net_offset[1]
        lon, lat = transformer.transform(x, y)
        return float(lon), float(lat)
    cxmin, cymin, cxmax, cymax = conv
    lomin, lamin, lomax, lamax = orig
    lon = lomin + (x - cxmin) * (lomax - lomin) / (cxmax - cxmin)
    lat = lamin + (y - cymin) * (lamax - lamin) / (cymax - cymin)
    return lon, lat


@dataclass
class EdgeGeom:
    edge_id: str
    function: str
    lonlat: list[tuple[float, float]]


def load_net_geoms(
    net_file: Path,
) -> tuple[list[EdgeGeom], tuple[float, float, float, float], tuple[float, float, float, float], str | None]:
    root = ET.parse(net_file).getroot()
    loc = root.find("location")
    if loc is None:
        raise RuntimeError("location tag missing")
    conv = tuple(float(x) for x in loc.attrib["convBoundary"].split(","))
    orig = tuple(float(x) for x in loc.attrib["origBoundary"].split(","))
    proj_param = loc.attrib.get("projParameter")
    net_offset = None
    if loc.attrib.get("netOffset"):
        ox, oy = [float(v) for v in loc.attrib["netOffset"].split(",")]
        net_offset = (ox, oy)
    transformer = parse_proj_transformer(proj_param)

    geoms: list[EdgeGeom] = []
    for edge in root.findall("edge"):
        eid = edge.attrib.get("id", "")
        fn = edge.attrib.get("function", "normal")
        lane = edge.find("lane")
        if lane is None:
            continue
        xy_pts = parse_shape(lane.attrib.get("shape", ""))
        if len(xy_pts) < 2:
            continue
        ll = [xy_to_lonlat(x, y, conv, orig, transformer, net_offset) for x, y in xy_pts]
        geoms.append(EdgeGeom(eid, fn, ll))

    return geoms, conv, orig, proj_param


def clip_segment_to_bbox(
    p1: tuple[float, float],
    p2: tuple[float, float],
    bbox: tuple[float, float, float, float],
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    xmin, ymin, xmax, ymax = bbox
    x1, y1 = p1
    x2, y2 = p2
    dx = x2 - x1
    dy = y2 - y1
    p = [-dx, dx, -dy, dy]
    q = [x1 - xmin, xmax - x1, y1 - ymin, ymax - y1]
    u1, u2 = 0.0, 1.0
    for pi, qi in zip(p, q):
        if pi == 0:
            if qi < 0:
                return None
            continue
        t = qi / pi
        if pi < 0:
            u1 = max(u1, t)
        else:
            u2 = min(u2, t)
    if u1 > u2:
        return None
    c1 = (x1 + u1 * dx, y1 + u1 * dy)
    c2 = (x1 + u2 * dx, y1 + u2 * dy)
    return c1, c2


def clipped_polyline_segments(
    lonlat: list[tuple[float, float]],
    bbox: tuple[float, float, float, float],
) -> list[list[tuple[float, float]]]:
    if len(lonlat) < 2:
        return []
    out: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for i in range(len(lonlat) - 1):
        clipped = clip_segment_to_bbox(lonlat[i], lonlat[i + 1], bbox)
        if clipped is None:
            if len(current) >= 2:
                out.append(current)
            current = []
            continue
        c1, c2 = clipped
        if not current:
            current = [c1, c2]
        else:
            if current[-1] != c1:
                current.append(c1)
            current.append(c2)
    if len(current) >= 2:
        out.append(current)
    return out


def project_to_canvas(lon: float, lat: float, min_lon: float, min_lat: float, max_lon: float, max_lat: float, w: int, h: int) -> tuple[float, float]:
    x = (lon - min_lon) / (max_lon - min_lon) * w
    y = (max_lat - lat) / (max_lat - min_lat) * h
    return x, y


def render_overlay(net_file: Path, validation_dir: Path, metadata_file: Path, out_png: Path, width: int, height: int) -> None:
    geoms, _, orig, _ = load_net_geoms(net_file)
    min_lon, min_lat, max_lon, max_lat = orig
    if metadata_file.exists():
        import json

        md = json.loads(metadata_file.read_text(encoding="utf-8"))
        mb = md.get("network_bbox")
        if mb:
            vals = [float(x) for x in str(mb).split(",")]
            if len(vals) == 4:
                min_lon, min_lat, max_lon, max_lat = vals
    bbox = (min_lon, min_lat, max_lon, max_lat)

    base, _ = build_basemap(min_lon, min_lat, max_lon, max_lat, width, height)

    disconnected_csv = validation_dir / "disconnected_edges.csv"
    disc = set()
    if disconnected_csv.exists() and disconnected_csv.stat().st_size > 0:
        ddf = pd.read_csv(disconnected_csv)
        disc = set(ddf["edge_id"].astype(str).tolist())

    matched_csv = validation_dir / "crosswalk_candidate_matching.csv"
    matched_cross = set()
    if matched_csv.exists() and matched_csv.stat().st_size > 0:
        mdf = pd.read_csv(matched_csv)
        matched_cross = set(mdf["matched_crossing_edge"].astype(str).tolist())

    fig = plt.figure(figsize=(width / 100, height / 100), dpi=100)
    ax = plt.axes([0, 0, 1, 1])
    ax.imshow(base)

    for g in geoms:
        clipped_parts = clipped_polyline_segments(g.lonlat, bbox)
        if not clipped_parts:
            continue

        for part in clipped_parts:
            pts = [project_to_canvas(lon, lat, min_lon, min_lat, max_lon, max_lat, width, height) for lon, lat in part]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            if g.function == "normal":
                # Primary simulation road network overlay (clear and readable)
                ax.plot(xs, ys, color="#0ea5e9", linewidth=1.1, alpha=0.75, solid_capstyle="round")
            elif g.function == "crossing":
                ax.plot(xs, ys, color="#0284c7", linewidth=1.0, alpha=0.8, solid_capstyle="round")
            elif g.edge_id in disc:
                # Keep disconnected visible but not dominant.
                ax.plot(xs, ys, color="#ef4444", linewidth=0.9, alpha=0.6, solid_capstyle="round")

    for g in geoms:
        if g.edge_id in matched_cross:
            clipped_parts = clipped_polyline_segments(g.lonlat, bbox)
            for part in clipped_parts:
                pts = [project_to_canvas(lon, lat, min_lon, min_lat, max_lon, max_lat, width, height) for lon, lat in part]
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                ax.plot(xs, ys, color="#2563eb", linewidth=2.4, alpha=0.95, solid_capstyle="round")
                cx, cy = np.mean(xs), np.mean(ys)
                ax.scatter([cx], [cy], s=35, c="#1d4ed8", edgecolors="white", linewidths=0.8, zorder=5)

    ax.set_axis_off()
    ax.text(18, 24, "Junggu simulation network overlay", fontsize=12, color="#0f172a", bbox=dict(facecolor="white", alpha=0.75, edgecolor="none", pad=4))
    ax.text(18, 44, "cyan: simulation roads, blue: matched crossing, red: disconnected", fontsize=9, color="#0f172a", bbox=dict(facecolor="white", alpha=0.65, edgecolor="none", pad=3))

    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=100)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--net_file", required=True)
    ap.add_argument("--validation_dir", required=True)
    ap.add_argument("--metadata_file", required=True)
    ap.add_argument("--out_png", required=True)
    ap.add_argument("--out_png_highres", required=True)
    args = ap.parse_args()

    render_overlay(Path(args.net_file), Path(args.validation_dir), Path(args.metadata_file), Path(args.out_png), 1800, 1400)
    render_overlay(Path(args.net_file), Path(args.validation_dir), Path(args.metadata_file), Path(args.out_png_highres), 3200, 2400)


if __name__ == "__main__":
    main()
