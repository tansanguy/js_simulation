#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

if [[ -z "${SUMO_HOME:-}" ]]; then
  for candidate in \
    /opt/homebrew/opt/sumo \
    /usr/local/opt/sumo \
    /usr/share/sumo \
    /Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO
  do
    if [[ -d "$candidate" ]]; then
      SUMO_HOME="$candidate"
      break
    fi
  done
fi
if [[ -z "${SUMO_HOME:-}" ]]; then
  echo "SUMO_HOME not found" >&2
  exit 1
fi
export SUMO_HOME
export PATH="$SUMO_HOME/bin:$PATH"
if [[ -z "${PROJ_LIB:-}" ]]; then
  if [[ -d "$SUMO_HOME/share/proj" ]]; then
    export PROJ_LIB="$SUMO_HOME/share/proj"
  elif [[ -d "$SUMO_HOME/proj" ]]; then
    export PROJ_LIB="$SUMO_HOME/proj"
  fi
fi
export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"

# TODO: 실제 위치 조정/생성 결과 폴더로 바꾸기
GEN_DIR="result/phase_next_bad22_generated_crossing_patch_YYYYMMDD_HHMMSS"

# TODO: 생성된 net.xml 경로로 바꾸기
NET_FILE="$GEN_DIR/generated_crossing_network.net.xml"

# TODO: 위치 조정 후 phase6 검증용 후보 CSV로 바꾸기
# 후보 전체 CSV가 있으면 이걸 사용
CANDIDATE_CSV="$GEN_DIR/generated_phase6_candidate_table.csv"

# 출력 폴더
OUT_ROOT="$GEN_DIR/seed1_smoke_validation"
mkdir -p "$OUT_ROOT/logs"
mkdir -p "$OUT_ROOT/single_candidates"

if [ ! -f "$NET_FILE" ]; then
  echo "Missing NET_FILE: $NET_FILE"
  exit 1
fi

if [ ! -f "$CANDIDATE_CSV" ]; then
  echo "Missing CANDIDATE_CSV: $CANDIDATE_CSV"
  exit 1
fi

echo "NET_FILE=$NET_FILE"
echo "CANDIDATE_CSV=$CANDIDATE_CSV"
echo "OUT_ROOT=$OUT_ROOT"

# 후보별 single candidate csv 생성
python3 - <<PY
from pathlib import Path
import pandas as pd
import re

cand_csv = Path("$CANDIDATE_CSV")
out_dir = Path("$OUT_ROOT/single_candidates")
out_dir.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(cand_csv)

if "crosswalk_id" not in df.columns:
    raise SystemExit("candidate csv must have crosswalk_id column")

for cid, g in df.groupby("crosswalk_id", dropna=False):
    cid = str(cid)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", cid)
    p = out_dir / f"{safe}.csv"
    g.to_csv(p, index=False)

print("single candidate csv count:", df["crosswalk_id"].nunique())
PY

# 후보별 seed 1 smart/baseline smoke
for CAND_CSV in "$OUT_ROOT"/single_candidates/*.csv; do
  CID=$(basename "$CAND_CSV" .csv)

  echo "========================================"
  echo "CANDIDATE=$CID"
  echo "========================================"

  for SCENARIO in smart baseline; do
    OUT_DIR="$OUT_ROOT/smoke_seed1_${SCENARIO}_${CID}"
    LOG_FILE="$OUT_ROOT/logs/smoke_seed1_${SCENARIO}_${CID}.log"

    if [ -f "$OUT_DIR/phase6_smoke_summary.csv" ]; then
      echo "[SKIP] $SCENARIO $CID already has summary"
      continue
    fi

    echo "[RUN] $SCENARIO $CID"

    python3 -m smart_crosswalk_sumo.run_phase6_recovery_smoke \
      --candidate-csv "$CAND_CSV" \
      --net-file "$NET_FILE" \
      --scenario "$SCENARIO" \
      --seed 1 \
      --sim-duration 600 \
      --warmup 0 \
      --step-length 0.5 \
      --extension-sec 5.0 \
      --phase-aligned-ped-depart \
      --ped-repeat-count 5 \
      --ped-repeat-spacing-sec 2 \
      --output-dir "$OUT_DIR" \
      > "$LOG_FILE" 2>&1
  done
done

echo "DONE"
echo "OUT_ROOT=$OUT_ROOT"
