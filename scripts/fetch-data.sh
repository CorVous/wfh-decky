#!/usr/bin/env bash
# Builds the bundled game data in data/ from the @wfcd/items npm package (MIT).
#
# Downloads the pinned tarball from registry.npmjs.org, checks its integrity,
# and trims data/json/*.json down to what the plugin reads (wfh.data.items
# build_bundle): ~29 MB of category JSON becomes
#   data/items.json.gz   ~240 KB  (~9.8k items, 608 relic rewards; 2.9 MB as JSON)
#   data/relics.json.gz   ~90 KB  (~800 relics with per-refinement chances)
#   data/manifest.json    version, source time, counts, sizes
# The worker refreshes this weekly from GitHub into its runtime dir; the bundle
# is the offline fallback. Bump VERSION (and SHA512) to update.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${WFCD_ITEMS_VERSION:-1.1276.18}"
SHA512="${WFCD_ITEMS_SHA512-AZSdF0nxOxayfts/QysDg3HVnpJGA5eQ2O2N4l3S5/EFZHq9aicGraFGnLHoWJesMBn94qKL0RinwMQ9h4cX2A==}"
OUT="$ROOT/data"

if [ -s "$OUT/manifest.json" ] && grep -q "\"version\": \"$VERSION\"" "$OUT/manifest.json" && [ -z "${FORCE:-}" ]; then
  echo "data/ already built from @wfcd/items $VERSION (FORCE=1 to rebuild)"
  exit 0
fi

PY="$ROOT/bin/python/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
TGZ="$WORK/items.tgz"
curl -sSfL -o "$TGZ" "https://registry.npmjs.org/@wfcd/items/-/items-$VERSION.tgz"
if [ -n "$SHA512" ]; then
  GOT="$(openssl dgst -sha512 -binary "$TGZ" | base64 -w0)"
  [ "$GOT" = "$SHA512" ] || { echo "integrity mismatch for @wfcd/items $VERSION" >&2; exit 1; }
fi
# Publish time = the source data's age; the runtime refresh only wins when newer.
SOURCE_TIME="$(curl -sSf "https://registry.npmjs.org/@wfcd/items" | "$PY" -I -c "import json,sys; print(json.load(sys.stdin)['time']['$VERSION'][:19] + 'Z')" || true)"

mkdir -p "$WORK/pkg"
tar -xzf "$TGZ" -C "$WORK/pkg" --wildcards 'package/data/json/*.json' --exclude='package/data/json/i18n*'
PYTHONPATH="$ROOT/py_modules" "$PY" -m wfh.data.items build "$WORK/pkg/package/data/json" "$OUT" "$VERSION" "$SOURCE_TIME"
ls -l "$OUT"
