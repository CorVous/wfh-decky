#!/usr/bin/env bash
# Assembles out/WFHelper.zip, installable from Decky's developer settings
# ("Install plugin from ZIP file") or by unzipping into ~/homebrew/plugins.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
NAME="WFHelper"
STAGE="$ROOT/out/stage/$NAME"

[ -x bin/python/bin/python3 ] || ./scripts/build-runtime.sh
./scripts/fetch-models.sh
./scripts/fetch-data.sh
pnpm run build

rm -rf "$ROOT/out/stage" "$ROOT/out/$NAME.zip"
mkdir -p "$STAGE/dist" "$STAGE/bin"
cp plugin.json package.json main.py LICENSE README.md "$STAGE/"
cp dist/index.js "$STAGE/dist/"
cp -r bin/python "$STAGE/bin/python"
# Decky extracts zips without symlinks; main.py runs bin/python3.12 directly.
find "$STAGE/bin/python" -type l -delete
rm -rf "$STAGE/bin/python/lib/pkgconfig"
mkdir -p "$STAGE/py_modules"
cp -r py_modules/wfh "$STAGE/py_modules/wfh"
find "$STAGE/py_modules" -name '__pycache__' -type d -prune -exec rm -rf {} +
[ -d models ] && cp -r models "$STAGE/models"
[ -d data ] && cp -r data "$STAGE/data"

(cd "$ROOT/out/stage" && zip -qr "../$NAME.zip" "$NAME")
ls -lh "$ROOT/out/$NAME.zip"
