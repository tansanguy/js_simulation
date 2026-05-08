#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path


TARGET_RELATION_ID = "2419947"


def ordered_ring(ways: dict[str, list[str]], outer_way_ids: list[str]) -> list[str]:
    remaining = {way_id: list(ways[way_id]) for way_id in outer_way_ids}
    if not remaining:
        raise ValueError("outer 경계 way가 없습니다.")

    first_way_id = next(iter(remaining))
    ring = remaining.pop(first_way_id)

    while remaining:
        tail = ring[-1]
        matched_way_id = None
        matched_nodes = None
        for way_id, nodes in remaining.items():
            if nodes[0] == tail:
                matched_way_id = way_id
                matched_nodes = nodes[1:]
                break
            if nodes[-1] == tail:
                matched_way_id = way_id
                matched_nodes = list(reversed(nodes[:-1]))
                break
        if matched_way_id is None or matched_nodes is None:
            raise ValueError("중구 경계 way를 하나의 닫힌 ring으로 연결하지 못했습니다.")
        ring.extend(matched_nodes)
        remaining.pop(matched_way_id)

    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def extract_boundary(osm_path: Path, output_path: Path) -> None:
    root = ET.parse(osm_path).getroot()
    node_coords = {
        node.attrib["id"]: [float(node.attrib["lon"]), float(node.attrib["lat"])]
        for node in root.findall("node")
    }
    ways = {
        way.attrib["id"]: [nd.attrib["ref"] for nd in way.findall("nd")]
        for way in root.findall("way")
    }

    target_relation = None
    for relation in root.findall("relation"):
        if relation.attrib.get("id") == TARGET_RELATION_ID:
            target_relation = relation
            break
    if target_relation is None:
        raise ValueError(f"relation {TARGET_RELATION_ID}를 찾지 못했습니다.")

    outer_way_ids = [
        member.attrib["ref"]
        for member in target_relation.findall("member")
        if member.attrib.get("type") == "way" and member.attrib.get("role") == "outer"
    ]
    missing = [way_id for way_id in outer_way_ids if way_id not in ways]
    if missing:
        raise ValueError(f"경계 way 일부가 누락되었습니다: {missing[:5]}")

    ring_node_ids = ordered_ring(ways, outer_way_ids)
    coordinates = [node_coords[node_id] for node_id in ring_node_ids]

    feature = {
        "type": "Feature",
        "properties": {
            "name": "중구",
            "name_en": "Jung-gu",
            "osm_relation_id": TARGET_RELATION_ID,
            "source": "OpenStreetMap",
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [coordinates],
        },
    }
    geojson = {"type": "FeatureCollection", "features": [feature]}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(geojson, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="OSM relation에서 중구 행정경계 GeoJSON 추출")
    parser.add_argument("--osm", required=True, help="relation 2419947이 포함된 OSM XML 경로")
    parser.add_argument("--output", required=True, help="출력 GeoJSON 경로")
    args = parser.parse_args()
    extract_boundary(Path(args.osm), Path(args.output))


if __name__ == "__main__":
    main()
