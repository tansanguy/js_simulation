#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SUMO_BIN="${SUMO_BIN:-$(command -v sumo-gui || true)}"
if [[ -z "$SUMO_BIN" ]]; then
  for candidate in \
    /opt/homebrew/opt/sumo/bin/sumo-gui \
    /usr/local/opt/sumo/bin/sumo-gui \
    /usr/share/sumo/bin/sumo-gui \
    /Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO/bin/sumo-gui
  do
    if [[ -x "$candidate" ]]; then
      SUMO_BIN="$candidate"
      break
    fi
  done
fi

SUMO_ROOT=""
if [[ -n "$SUMO_BIN" ]]; then
  SUMO_ROOT="$(cd "$(dirname "$SUMO_BIN")/.." && pwd)"
fi

PROJ_LIB_DYLIB="${PROJ_LIB_DYLIB:-}"
FONTCONFIG_DYLIB="${FONTCONFIG_DYLIB:-}"
if [[ -n "$SUMO_ROOT" ]]; then
  PROJ_LIB_DYLIB="${PROJ_LIB_DYLIB:-$SUMO_ROOT/lib/libproj.25.9.5.1.dylib}"
  FONTCONFIG_DYLIB="${FONTCONFIG_DYLIB:-$SUMO_ROOT/lib/libfontconfig.1.dylib}"
fi

echo "[sumo]"
if [[ -x "$SUMO_BIN" ]]; then
  echo "sumo-gui: $SUMO_BIN"
  "$SUMO_BIN" --version | head -n 1
else
  echo "sumo-gui not found: $SUMO_BIN"
fi

echo
echo "[install-names]"
if [[ -f "$PROJ_LIB_DYLIB" ]]; then
  otool -D "$PROJ_LIB_DYLIB" | tail -n +2
fi
if [[ -f "$FONTCONFIG_DYLIB" ]]; then
  otool -D "$FONTCONFIG_DYLIB" | tail -n +2
fi

echo
echo "[expected runtime data]"
for path in \
  "${PROJ_LIB:-/opt/homebrew/share/proj}/proj.db" \
  "${FONTCONFIG_PATH:-/opt/homebrew/etc/fonts}/fonts.conf"
do
  if [[ -f "$path" ]]; then
    echo "OK $path"
  else
    echo "MISSING $path"
  fi
done

echo
echo "[brew packages]"
if command -v brew >/dev/null 2>&1; then
  brew info proj fontconfig 2>/dev/null | sed -n '1,60p'
fi

echo
echo "[recommended fix]"
echo "brew install proj fontconfig"
echo "fc-cache -fv"
