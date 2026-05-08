from __future__ import annotations

import argparse
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pandas as pd

try:
    import traci  # type: ignore
except ImportError:
    traci = None


def ensure_gui_settings(path: Path) -> None:
    root = ET.Element("viewsettings")
    ET.SubElement(root, "viewport", {"zoom": "1000", "x": "0", "y": "0"})
    ET.SubElement(root, "delay", {"value": "0"})
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def copy_replay_sumocfg(cw_dir: Path, scenario: str, seed: int, replay_dir: Path) -> Path | None:
    src = cw_dir / f"{scenario}_seed{seed}.sumocfg"
    if not src.exists():
        return None
    dst = replay_dir / f"replay_{scenario}_seed{seed}.sumocfg"
    shutil.copy2(src, dst)
    return dst


def maybe_capture_snapshot(sumocfg: Path, screenshot_path: Path, snapshot_time: float = 300.0) -> str:
    if traci is None:
        return "traci_unavailable"
    try:
        screenshot_abs = screenshot_path.resolve()
        traci.start(["sumo-gui", "-c", str(sumocfg.resolve()), "--start", "--quit-on-end"])
        while traci.simulation.getTime() < snapshot_time:
            traci.simulationStep()
        traci.gui.screenshot("View #0", str(screenshot_abs))
        traci.simulationStep()
        traci.close(False)
        return "ok" if screenshot_abs.exists() else "snapshot_missing"
    except Exception as exc:
        try:
            traci.close(False)
        except Exception:
            pass
        return f"snapshot_failed:{exc}"


def write_conflict_overlay(candidates: pd.DataFrame, out_path: Path) -> None:
    features = []
    for row in candidates.itertuples(index=False):
        if not hasattr(row, "lon") or not hasattr(row, "lat"):
            continue
        lon = float(getattr(row, "lon"))
        lat = float(getattr(row, "lat"))
        delta = 0.00008
        ring = [
            [lon - delta, lat - delta],
            [lon + delta, lat - delta],
            [lon + delta, lat + delta],
            [lon - delta, lat + delta],
            [lon - delta, lat - delta],
        ]
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "crosswalk_id": str(getattr(row, "횡단보도ID", "")),
                    "type": "conflict_area",
                },
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )
    geojson = {"type": "FeatureCollection", "features": features}
    out_path.write_text(json.dumps(geojson, ensure_ascii=False, indent=2), encoding="utf-8")


def write_queue_heatmap(output_dir: Path, figures_dir: Path, candidates: pd.DataFrame) -> None:
    edge_files = list(output_dir.glob("edge_data_*_seed*.xml"))
    queue_values = []
    for path in edge_files:
        try:
            root = ET.parse(path).getroot()
        except Exception:
            continue
        for edge in root.findall("edge"):
            queue_values.append(float(edge.attrib.get("queue", "0")))

    avg_queue = sum(queue_values) / len(queue_values) if queue_values else 0.0
    if candidates.empty or not {"lat", "lon"}.issubset(candidates.columns):
        return
    center_lat = float(candidates["lat"].mean())
    center_lon = float(candidates["lon"].mean())
    markers = candidates[["횡단보도ID", "lat", "lon"]].to_dict(orient="records")

    html = f"""<!doctype html>
<html lang=\"ko\">
<head>
  <meta charset=\"utf-8\">
  <title>Queue Heatmap</title>
  <link rel=\"stylesheet\" href=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css\">
  <script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\"></script>
  <style>#map {{ height: 92vh; }} body {{ margin: 0; }}</style>
</head>
<body>
<div id=\"map\"></div>
<script>
const map = L.map('map').setView([{center_lat}, {center_lon}], 13);
L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom: 19}}).addTo(map);
const markers = {json.dumps(markers, ensure_ascii=False)};
const avgQueue = {avg_queue:.4f};
for (const m of markers) {{
  const r = 4 + Math.min(12, avgQueue);
  L.circleMarker([m.lat, m.lon], {{radius: r, color: '#e6550d', fillOpacity: 0.6}})
    .bindPopup(`횡단보도ID: ${{m["횡단보도ID"]}}<br>avg_queue_proxy: ${{avgQueue.toFixed(2)}}`)
    .addTo(map);
}}
</script>
</body>
</html>
"""
    (figures_dir / "queue_heatmap.html").write_text(html, encoding="utf-8")


def export_visual_assets(
    output_dir: str | Path,
    nets_dir: str | Path,
    figures_dir: str | Path,
    candidates_csv: str | Path,
    seeds: tuple[int, ...],
) -> None:
    output_dir = Path(output_dir)
    nets_dir = Path(nets_dir)
    figures_dir = Path(figures_dir)
    replay_dir = figures_dir / "sumo_replay"
    snapshots_dir = figures_dir / "baseline_vs_smart_snapshots"
    replay_dir.mkdir(parents=True, exist_ok=True)
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    candidates = pd.read_csv(candidates_csv) if Path(candidates_csv).exists() else pd.DataFrame()

    gui_settings = replay_dir / "gui-settings.xml"
    ensure_gui_settings(gui_settings)

    statuses = []
    for row in candidates.itertuples(index=False):
        cw_id = getattr(row, "횡단보도ID")
        cw_dir = nets_dir / f"cw_{cw_id}"
        for seed in seeds:
            for scenario in ("baseline", "smart"):
                replay_cfg = copy_replay_sumocfg(cw_dir, scenario, seed, replay_dir)
                if replay_cfg is None:
                    continue
                screenshot_path = snapshots_dir / f"cw_{cw_id}_{scenario}_seed{seed}.png"
                status = maybe_capture_snapshot(replay_cfg, screenshot_path)
                statuses.append(
                    {
                        "crosswalk_id": cw_id,
                        "seed": seed,
                        "scenario": scenario,
                        "snapshot": str(screenshot_path),
                        "status": status,
                    }
                )

    if statuses:
        pd.DataFrame(statuses).to_csv(output_dir / "snapshot_export_status.csv", index=False)

    write_conflict_overlay(candidates, output_dir / "conflict_area_overlay.geojson")
    write_queue_heatmap(output_dir, figures_dir, candidates)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output_dir", default="outputs")
    parser.add_argument("--nets_dir", default="sumo_nets")
    parser.add_argument("--figures_dir", default="figures")
    parser.add_argument("--candidates", default="outputs/candidates.csv")
    parser.add_argument("--seeds", nargs="+", type=int, default=[42])
    args = parser.parse_args()
    export_visual_assets(
        args.output_dir,
        args.nets_dir,
        args.figures_dir,
        args.candidates,
        tuple(args.seeds),
    )


if __name__ == "__main__":
    main()
