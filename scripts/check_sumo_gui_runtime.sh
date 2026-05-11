#!/usr/bin/env bash
set -euo pipefail

SUMO_BIN="/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO/bin/sumo-gui"
PROJ_LIB_DYLIB="/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO/lib/libproj.25.9.5.1.dylib"
FONTCONFIG_DYLIB="/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/EclipseSUMO/lib/libfontconfig.1.dylib"

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
  /opt/homebrew/share/proj/proj.db \
  /opt/homebrew/etc/fonts/fonts.conf
do
  if [[ -f "$path" ]]; then
    echo "OK $path"
  else
    echo "MISSING $path"
  fi
done

echo
echo "[brew packages]"
brew info proj fontconfig 2>/dev/null | sed -n '1,60p'

echo
echo "[recommended fix]"
echo "brew install proj fontconfig"
echo "fc-cache -fv"
