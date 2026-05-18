"""
중구 OSM 네트워크 생성 스크립트
T2 좌표를 커버하는 georeferenced SUMO network를 OpenStreetMap에서 생성합니다.
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

from smart_crosswalk_sumo.network_utils import resolve_sumo_home

try:
    import sumolib
except ImportError:
    sumolib = None


class T2OSMNetworkBuilder:
    def __init__(self, t2_csv_path: str, output_base: str = "result"):
        self.t2_path = Path(t2_csv_path)
        self.output_base = Path(output_base)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = self.output_base / f"junggu_osm_network_{timestamp}"

        self.t2_df = None
        self.bbox = None
        self.buffered_bbox = None

    def load_t2_csv(self) -> None:
        """T2 CSV 읽기"""
        print(f"[1/7] T2 CSV 읽기: {self.t2_path}")
        self.t2_df = pd.read_csv(self.t2_path)
        print(f"  rows: {len(self.t2_df)}")
        print(f"  lon range: {self.t2_df['lon'].min():.6f} ~ {self.t2_df['lon'].max():.6f}")
        print(f"  lat range: {self.t2_df['lat'].min():.6f} ~ {self.t2_df['lat'].max():.6f}")

    def calculate_bbox(self, buffer_m: float = 1000.0) -> None:
        """T2 좌표 bbox 계산 및 buffer 추가"""
        print(f"\n[2/7] T2 bbox 계산 (buffer {buffer_m}m)")

        lon_min, lon_max = self.t2_df['lon'].min(), self.t2_df['lon'].max()
        lat_min, lat_max = self.t2_df['lat'].min(), self.t2_df['lat'].max()

        self.bbox = (lon_min, lat_min, lon_max, lat_max)

        # 약 111km per degree로 근사 (lat 기준)
        lat_buffer = buffer_m / 111000.0
        lon_buffer = buffer_m / (111000.0 * 0.8)  # lon은 위도에 따라 다름

        buffered_lon_min = lon_min - lon_buffer
        buffered_lon_max = lon_max + lon_buffer
        buffered_lat_min = lat_min - lat_buffer
        buffered_lat_max = lat_max + lat_buffer

        self.buffered_bbox = (buffered_lon_min, buffered_lat_min, buffered_lon_max, buffered_lat_max)

        print(f"  Original bbox (lon, lat):")
        print(f"    west={self.bbox[0]:.6f}, south={self.bbox[1]:.6f}")
        print(f"    east={self.bbox[2]:.6f}, north={self.bbox[3]:.6f}")
        print(f"  Buffered bbox:")
        print(f"    west={self.buffered_bbox[0]:.6f}, south={self.buffered_bbox[1]:.6f}")
        print(f"    east={self.buffered_bbox[2]:.6f}, north={self.buffered_bbox[3]:.6f}")

        # bbox를 JSON으로 저장
        self.output_dir.mkdir(parents=True, exist_ok=True)
        bbox_json = {
            "original": {
                "west": float(self.bbox[0]),
                "south": float(self.bbox[1]),
                "east": float(self.bbox[2]),
                "north": float(self.bbox[3]),
            },
            "buffered": {
                "west": float(self.buffered_bbox[0]),
                "south": float(self.buffered_bbox[1]),
                "east": float(self.buffered_bbox[2]),
                "north": float(self.buffered_bbox[3]),
            },
        }

        with open(self.output_dir / "t2_bbox.json", "w") as f:
            json.dump(bbox_json, f, indent=2)
        print(f"  saved: {self.output_dir / 't2_bbox.json'}")

    def download_osm(self) -> Path:
        """Overpass API를 사용해 OSM 다운로드"""
        print(f"\n[3/7] OSM 다운로드")

        osm_path = self.output_dir / "downloaded_junggu.osm.xml"

        bbox_str = f"{self.buffered_bbox[0]},{self.buffered_bbox[1]},{self.buffered_bbox[2]},{self.buffered_bbox[3]}"

        # osmGet.py 사용
        sumo_home = resolve_sumo_home()
        if not sumo_home:
            raise RuntimeError(
                "SUMO_HOME not found. Set SUMO_HOME or install SUMO in a standard location."
            )
        osmget_path = None
        for candidate in [
            Path(sumo_home) / "tools" / "osmGet.py",
            Path(sumo_home) / "share" / "sumo" / "tools" / "osmGet.py",
        ]:
            if candidate.exists():
                osmget_path = candidate
                break

        if osmget_path:
            print(f"  using osmGet.py: {osmget_path}")
            cmd = [
                sys.executable,
                str(osmget_path),
                "-b", bbox_str,
                "-p", str(self.output_dir / "junggu"),
                "-v",
            ]

            print(f"  command: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                # osmGet.py는 {prefix}.osm.xml로 저장
                downloaded = self.output_dir / "junggu.osm.xml"
                if downloaded.exists():
                    # 이름 변경
                    downloaded.rename(osm_path)
                    print(f"  ✓ OSM downloaded: {osm_path}")
                    print(f"  file size: {osm_path.stat().st_size / 1024 / 1024:.2f} MB")
                    return osm_path

            print(f"  osmGet.py result: {result.returncode}")
            if result.stdout:
                print(f"  stdout:\n{result.stdout[:500]}")
            if result.stderr:
                print(f"  stderr:\n{result.stderr[:500]}")

        # Fallback: Overpass API 직접 사용
        print(f"  fallback: using Overpass API directly")
        import requests

        # 여러 Overpass 서버 시도
        overpass_urls = [
            "https://overpass-api.de/api/map",
            "https://lz4.overpass-api.de/api/map",
            "https://z.overpass-api.de/api/map",
            "https://overpass.kumi.systems/api/map",
        ]

        for idx, overpass_url in enumerate(overpass_urls, 1):
            try:
                print(f"  trying Overpass server {idx}/4: {overpass_url}")

                headers = {
                    "User-Agent": "Mozilla/5.0 (compatible; SUMO-crosswalk-gen/1.0)"
                }

                # bbox 형식: west,south,east,north
                response = requests.get(
                    overpass_url,
                    params={"bbox": bbox_str},
                    headers=headers,
                    timeout=120
                )

                if response.status_code == 200:
                    osm_path.write_text(response.text, encoding='utf-8')
                    print(f"  ✓ OSM downloaded from {overpass_url}")
                    print(f"  file size: {osm_path.stat().st_size / 1024 / 1024:.2f} MB")
                    return osm_path
                else:
                    print(f"    status: {response.status_code} {response.reason}")

            except Exception as e:
                print(f"    error: {e}")
                continue

        print(f"  ✗ All Overpass servers failed")
        raise RuntimeError(f"Failed to download OSM from Overpass API")

    def run_netconvert_direct(self, osm_path: Path) -> Path:
        """netconvert를 직접 실행해 OSM을 SUMO network로 변환"""
        import os

        print(f"\n[4/7] netconvert 실행 (pedestrian infrastructure 포함)")

        net_path = self.output_dir / "junggu_generated.net.xml"
        netconvert_log = self.output_dir / "netconvert.log"

        # 절대 경로 사용
        osm_abs = osm_path.resolve()
        net_abs = net_path.resolve()

        # SUMO_HOME 설정 (macOS Framework 구조)
        env = os.environ.copy()
        env["SUMO_HOME"] = sumo_home
        env["PATH"] = f"{Path(sumo_home) / 'bin'}:{env.get('PATH', '')}"

        # netconvert 명령
        cmd = [
            "netconvert",
            "--osm-files", str(osm_abs),
            "--output-file", str(net_abs),
            "--geometry.remove=true",
            "--roundabouts.guess=true",
            "--ramps.guess=true",
            "--tls.guess=true",
            "--sidewalks.guess=true",
            "--crossings.guess=true",
        ]

        print(f"  command: netconvert --osm-files ... --output-file ...")
        print(f"  SUMO_HOME: {env.get('SUMO_HOME', '(unset)')}")
        print(f"  OSM file: {osm_abs}")
        print(f"  Output: {net_abs}")

        with open(netconvert_log, "w") as log_f:
            result = subprocess.run(cmd, capture_output=True, text=True, env=env)
            log_f.write(f"SUMO_HOME: {env.get('SUMO_HOME', '(unset)')}\n")
            log_f.write(f"command: {' '.join(cmd)}\n")
            log_f.write(f"returncode: {result.returncode}\n")
            log_f.write(f"stdout:\n{result.stdout[-2000:]}\n")  # 마지막 2000자만
            log_f.write(f"stderr:\n{result.stderr[-2000:]}\n")

            print(f"  returncode: {result.returncode}")

        if result.returncode == 0 and net_path.exists():
            print(f"  ✓ netconvert succeeded")
            print(f"  output size: {net_path.stat().st_size / 1024 / 1024:.1f} MB")
            return net_path
        else:
            print(f"  ✗ netconvert failed")
            print(f"  see: {netconvert_log}")
            if result.stderr:
                lines = result.stderr.split('\n')
                print(f"  last 10 lines of stderr:")
                for line in lines[-10:]:
                    if line.strip():
                        print(f"    {line}")
            raise RuntimeError(f"netconvert failed")

    def validate_network(self, net_path: Path) -> dict:
        """생성된 네트워크 검증"""
        print(f"\n[5/7] 네트워크 검증")

        if not sumolib:
            print(f"  ⚠ sumolib not available, skipping sumolib validation")
            return {}

        try:
            net = sumolib.net.readNet(str(net_path))
        except Exception as e:
            print(f"  ✗ Failed to load network: {e}")
            raise

        bbox = net.getBBoxXY()
        print(f"  network bbox (SUMO XY): {bbox}")

        # T2 좌표 검증
        converted = 0
        inside = 0
        rows = []

        for _, r in self.t2_df.iterrows():
            cid = r["횡단보도ID"]
            lon = float(r["lon"])
            lat = float(r["lat"])

            try:
                x, y = net.convertLonLat2XY(lon, lat)
                converted += 1
                ok = bbox[0][0] <= x <= bbox[1][0] and bbox[0][1] <= y <= bbox[1][1]
                inside += int(ok)
                rows.append({
                    "crosswalk_id": cid,
                    "lon": lon,
                    "lat": lat,
                    "x": x,
                    "y": y,
                    "inside_bbox": ok,
                })
            except Exception as e:
                rows.append({
                    "crosswalk_id": cid,
                    "lon": lon,
                    "lat": lat,
                    "x": None,
                    "y": None,
                    "inside_bbox": False,
                    "error": str(e),
                })

        # 119055 검증
        r119055 = self.t2_df[self.t2_df["횡단보도ID"].astype(str) == "119055"]
        if not r119055.empty:
            r = r119055.iloc[0]
            try:
                x119, y119 = net.convertLonLat2XY(float(r["lon"]), float(r["lat"]))
                inside119 = bbox[0][0] <= x119 <= bbox[1][0] and bbox[0][1] <= y119 <= bbox[1][1]
            except Exception as e:
                x119, y119, inside119 = None, None, False
        else:
            x119, y119, inside119 = None, None, False

        # 네트워크 구조 정보
        num_nodes = len(net.getNodes())
        num_edges = len(net.getEdges())
        num_tls = len(net.getTrafficLights())

        # T2 CSV validation 저장
        validation_df = pd.DataFrame(rows)
        validation_csv = self.output_dir / "network_validation.csv"
        validation_df.to_csv(validation_csv, index=False)

        coverage = inside / len(self.t2_df) if len(self.t2_df) else 0

        print(f"  T2 converted: {converted}/{len(self.t2_df)}")
        print(f"  T2 inside bbox: {inside}/{len(self.t2_df)} ({coverage*100:.1f}%)")
        print(f"  119055 xy: ({x119}, {y119})")
        print(f"  119055 inside bbox: {inside119}")
        print(f"  network nodes: {num_nodes}")
        print(f"  network edges: {num_edges}")
        print(f"  network tls: {num_tls}")

        validation_result = {
            "net_path": str(net_path),
            "bbox": bbox,
            "t2_total": len(self.t2_df),
            "t2_converted": converted,
            "t2_inside": inside,
            "t2_coverage": coverage,
            "case_119055_xy": (x119, y119),
            "case_119055_inside": inside119,
            "nodes": num_nodes,
            "edges": num_edges,
            "tls": num_tls,
            "validation_csv": str(validation_csv),
        }

        return validation_result

    def generate_case_119055_report(self, net_path: Path, validation: dict) -> None:
        """119055 전용 리포트"""
        print(f"\n[6/7] Case 119055 리포트 생성")

        if not sumolib:
            print(f"  ⚠ sumolib not available, skipping detailed analysis")
            return

        try:
            net = sumolib.net.readNet(str(net_path))
        except Exception:
            return

        r119055 = self.t2_df[self.t2_df["횡단보도ID"].astype(str) == "119055"]
        if r119055.empty:
            print(f"  119055 not found in T2 CSV")
            return

        r = r119055.iloc[0]
        lon, lat = float(r["lon"]), float(r["lat"])

        try:
            x, y = net.convertLonLat2XY(lon, lat)
        except Exception:
            x, y = None, None

        bbox = net.getBBoxXY()
        inside = x is not None and bbox[0][0] <= x <= bbox[1][0] and bbox[0][1] <= y <= bbox[1][1]

        # Nearest junction
        nearest_junc = None
        min_dist = float('inf')

        for node in net.getNodes():
            if x is not None and y is not None:
                dist = ((node.x - x)**2 + (node.y - y)**2)**0.5
                if dist < min_dist:
                    min_dist = dist
                    nearest_junc = (node.id, dist)

        report = f"""# Case 119055 Network Analysis

## 좌표 정보
- CSV ID: 119055
- 위치: 순화동 (을지로동)
- WGS84 좌표: lon={lon:.6f}, lat={lat:.6f}

## SUMO 변환
- SUMO XY: ({x:.2f}, {y:.2f}) if x else "변환 실패"
- 네트워크 bbox 포함 여부: {inside}

## 네트워크 정보
- Network bbox: {bbox}
- Nodes: {validation.get('nodes', '?')}
- Edges: {validation.get('edges', '?')}
- TLS: {validation.get('tls', '?')}

## Nearest Junction
- Junction ID: {nearest_junc[0] if nearest_junc else "없음"}
- Distance: {nearest_junc[1]:.2f}m if nearest_junc else "N/A"

## 교차로 생성 가능 여부
- SUMO 네트워크 내 위치: {"✓ Yes" if inside else "✗ No"}
- Nearest junction 거리: {nearest_junc[1]:.2f}m if nearest_junc else "거리 계산 불가"

## 권고사항
"""

        if inside and nearest_junc and nearest_junc[1] < 100:
            report += "- ✓ 이 위치에서 crossing 생성 가능\n"
        else:
            report += "- ✗ 이 위치에서 crossing 생성 불가능\n"

        case119055_path = self.output_dir / "case_119055_network_check.md"
        case119055_path.write_text(report, encoding='utf-8')
        print(f"  saved: {case119055_path}")

    def generate_summary_report(self, validation: dict) -> None:
        """종합 검증 리포트"""
        print(f"\n[7/7] 종합 리포트 생성")

        summary = f"""# 중구 OSM SUMO 네트워크 생성 결과

## 생성 정보
- 생성 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- Output directory: {self.output_dir}

## T2 CSV 정보
- 횡단보도 개수: {validation.get('t2_total', '?')}
- 좌표 변환 가능: {validation.get('t2_converted', '?')}/{validation.get('t2_total', '?')}
- 네트워크 bbox 내: {validation.get('t2_inside', '?')}/{validation.get('t2_total', '?')}
- 커버리지: {validation.get('t2_coverage', 0)*100:.1f}%

## 네트워크 정보
- Network path: {validation.get('net_path', '?')}
- Bbox (SUMO XY): {validation.get('bbox', '?')}
- Nodes: {validation.get('nodes', '?')}
- Edges: {validation.get('edges', '?')}
- TLS: {validation.get('tls', '?')}

## Case 119055
- SUMO XY: ({validation.get('case_119055_xy', (None, None))[0]}, {validation.get('case_119055_xy', (None, None))[1]})
- Inside bbox: {validation.get('case_119055_inside', False)}

## 다음 단계 가능 여부
"""

        coverage = validation.get('t2_coverage', 0)
        inside119 = validation.get('case_119055_inside', False)

        if coverage >= 0.95 and inside119:
            summary += """
### ✓ Crossing Generation 진행 가능

다음 스크립트로 T2 좌표 기반 crossing 생성:
```bash
python3 smart_crosswalk_sumo/generate_crosswalks_from_t2.py \\
  --base-network {net_path} \\
  --t2-csv smart_crosswalk_sumo/data/T2_crosswalk_features.csv \\
  --output result/t2_generated_crosswalks_<timestamp>
```
""".format(net_path=validation.get('net_path', ''))
        else:
            summary += f"""
### ✗ Crossing Generation 진행 불가능

이유:
- T2 커버리지: {coverage*100:.1f}% (필요: >= 95%)
- Case 119055 inside: {inside119} (필요: True)

조치:
1. 네트워크 bbox를 확장
2. OSM 데이터 재다운로드
3. netconvert 옵션 조정
"""

        summary_path = self.output_dir / "network_validation_summary.md"
        summary_path.write_text(summary, encoding='utf-8')
        print(f"  saved: {summary_path}")

    def run(self) -> bool:
        """전체 파이프라인 실행"""
        try:
            self.load_t2_csv()
            self.calculate_bbox(buffer_m=1000.0)

            osm_path = self.download_osm()

            net_path = self.run_netconvert_direct(osm_path)

            validation = self.validate_network(net_path)

            self.generate_case_119055_report(net_path, validation)

            self.generate_summary_report(validation)

            print(f"\n{'='*60}")
            print(f"✓ 완료: {self.output_dir}")
            print(f"{'='*60}")

            return True

        except Exception as e:
            print(f"\n✗ 오류: {e}")
            import traceback
            traceback.print_exc()
            return False


if __name__ == "__main__":
    builder = T2OSMNetworkBuilder(
        t2_csv_path="smart_crosswalk_sumo/data/T2_crosswalk_features.csv",
        output_base="result",
    )
    success = builder.run()
    sys.exit(0 if success else 1)
