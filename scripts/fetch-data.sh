#!/usr/bin/env bash
# Builds the bundled game data in data/ from the @wfcd/items npm package (MIT).
#
# Downloads the pinned tarball from registry.npmjs.org, checks its integrity,
# and trims data/json/*.json down to what the plugin reads (wfh.data.items
# build_bundle): ~29 MB of category JSON becomes
#   data/items.json.gz   ~240 KB  (~9.8k items, 608 relic rewards; 2.9 MB as JSON)
#   data/relics.json.gz   ~90 KB  (~800 relics with per-refinement chances)
#   data/manifest.json    version, source time, counts, sizes, and source_sha256
#                         of Relics.json/Components.json (byte-identical to the
#                         v$VERSION tag on GitHub)
# The worker refreshes this weekly from GitHub into its runtime dir; the bundle
# is the offline fallback. Its first check downloads only Relics.json and
# Components.json (~13 MB raw) and skips the full ~29 MB when their sha256 still
# matches source_sha256. Bump VERSION (and SHA512) to update.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${WFCD_ITEMS_VERSION:-1.1276.18}"
SHA512="${WFCD_ITEMS_SHA512-AZSdF0nxOxayfts/QysDg3HVnpJGA5eQ2O2N4l3S5/EFZHq9aicGraFGnLHoWJesMBn94qKL0RinwMQ9h4cX2A==}"
OUT="$ROOT/data"

# Riven data (weapon dispositions, riven stat pools, prefixes) from the
# warframe-public-export-plus npm package (MIT), trimmed by
# wfh.features.riven_data to data/riven/riven.json.gz (~15 KB). Independent of
# @wfcd/items below; RIVEN_PEP_VERSION/RIVEN_PEP_SHA512 pin it, FORCE=1 rebuilds.
(
  PEP_VERSION="${RIVEN_PEP_VERSION:-0.6.12}"
  PEP_SHA512="${RIVEN_PEP_SHA512-qmFNMwGfcbCs1Guli/DZ3l5BBNXTCgH+7Khxk7rBK1bCXAuLLG51GFRTEPxju5k6aDdVOzr9eshTSce/xJh5rw==}"
  RPY="$ROOT/bin/python/bin/python3"
  [ -x "$RPY" ] || RPY="$(command -v python3)"
  HAVE="$("$RPY" -I -c "import gzip,json,sys; print(json.load(gzip.open(sys.argv[1]))['version'])" "$OUT/riven/riven.json.gz" 2>/dev/null || true)"
  if [ "$HAVE" = "$PEP_VERSION" ] && [ -z "${FORCE:-}" ]; then
    echo "data/riven already built from warframe-public-export-plus $PEP_VERSION"
    exit 0
  fi
  RWORK="$(mktemp -d)"
  trap 'rm -rf "$RWORK"' EXIT
  curl -sSfL -o "$RWORK/pep.tgz" "https://registry.npmjs.org/warframe-public-export-plus/-/warframe-public-export-plus-$PEP_VERSION.tgz"
  if [ -n "$PEP_SHA512" ]; then
    GOT="$(openssl dgst -sha512 -binary "$RWORK/pep.tgz" | base64 -w0)"
    [ "$GOT" = "$PEP_SHA512" ] || { echo "integrity mismatch for warframe-public-export-plus $PEP_VERSION" >&2; exit 1; }
  fi
  mkdir -p "$RWORK/pkg"
  tar -xzf "$RWORK/pep.tgz" -C "$RWORK/pkg" package/ExportWeapons.json package/ExportUpgrades.json package/dict.en.json package/package.json
  PYTHONPATH="$ROOT/py_modules" "$RPY" -m wfh.features.riven_data build "$RWORK/pkg/package" "$OUT/riven"
)

# Mastery data (which items grant mastery, rank caps, blueprints/Market availability,
# star chart node mastery) for the Mastery page, wfh.menus.mastery build:
# data/mastery.json.gz (~32 KB) from warframe-public-export-plus (same pin as the
# riven data) + @wfcd/items ($VERSION, `masterable`/`exalted`/vault flags). FORCE=1 rebuilds.
(
  PEP_VERSION="${RIVEN_PEP_VERSION:-0.6.12}"
  PEP_SHA512="${RIVEN_PEP_SHA512-qmFNMwGfcbCs1Guli/DZ3l5BBNXTCgH+7Khxk7rBK1bCXAuLLG51GFRTEPxju5k6aDdVOzr9eshTSce/xJh5rw==}"
  MLABEL="pep $PEP_VERSION + wfcd $VERSION m2"  # m2: known30 field
  MPY="$ROOT/bin/python/bin/python3"
  [ -x "$MPY" ] || MPY="$(command -v python3)"
  HAVE="$("$MPY" -I -c "import gzip,json,sys; print(json.load(gzip.open(sys.argv[1]))['version'])" "$OUT/mastery.json.gz" 2>/dev/null || true)"
  if [ "$HAVE" = "$MLABEL" ] && [ -z "${FORCE:-}" ]; then
    echo "data/mastery.json.gz already built from $MLABEL"
    exit 0
  fi
  MWORK="$(mktemp -d)"
  trap 'rm -rf "$MWORK"' EXIT
  curl -sSfL -o "$MWORK/pep.tgz" "https://registry.npmjs.org/warframe-public-export-plus/-/warframe-public-export-plus-$PEP_VERSION.tgz"
  if [ -n "$PEP_SHA512" ]; then
    GOT="$(openssl dgst -sha512 -binary "$MWORK/pep.tgz" | base64 -w0)"
    [ "$GOT" = "$PEP_SHA512" ] || { echo "integrity mismatch for warframe-public-export-plus $PEP_VERSION" >&2; exit 1; }
  fi
  curl -sSfL -o "$MWORK/items.tgz" "https://registry.npmjs.org/@wfcd/items/-/items-$VERSION.tgz"
  if [ -n "$SHA512" ]; then
    GOT="$(openssl dgst -sha512 -binary "$MWORK/items.tgz" | base64 -w0)"
    [ "$GOT" = "$SHA512" ] || { echo "integrity mismatch for @wfcd/items $VERSION" >&2; exit 1; }
  fi
  mkdir -p "$MWORK/pep" "$MWORK/wfcd"
  tar -xzf "$MWORK/pep.tgz" -C "$MWORK/pep" package/ExportWarframes.json package/ExportWeapons.json \
    package/ExportSentinels.json package/ExportRecipes.json package/ExportRegions.json package/dict.en.json package/package.json
  tar -xzf "$MWORK/items.tgz" -C "$MWORK/wfcd" package/data/json/Warframes.json package/data/json/Primary.json \
    package/data/json/Secondary.json package/data/json/Melee.json package/data/json/Sentinels.json \
    package/data/json/Pets.json package/data/json/Archwing.json package/data/json/Arch-Gun.json \
    package/data/json/Arch-Melee.json package/data/json/Misc.json
  mkdir -p "$OUT"
  PYTHONPATH="$ROOT/py_modules" "$MPY" -m wfh.menus.mastery build "$MWORK/pep/package" "$MWORK/wfcd/package/data/json" "$OUT" "$MLABEL"
)

# Foundry data (blueprint -> product recipes with ingredients, credits, build time,
# reusable blueprints; DE productCategory; mod/arcane max rank; English names of
# recipe items @wfcd lacks) from the same warframe-public-export-plus package,
# trimmed by wfh.inventory_view to data/foundry/foundry.json.gz (~160 KB). Used by
# the Inventory and Foundry pages; FOUNDRY_PEP_VERSION/FOUNDRY_PEP_SHA512 pin it.
(
  FPEP_VERSION="${FOUNDRY_PEP_VERSION:-${RIVEN_PEP_VERSION:-0.6.12}}"
  FPEP_SHA512="${FOUNDRY_PEP_SHA512-${RIVEN_PEP_SHA512-qmFNMwGfcbCs1Guli/DZ3l5BBNXTCgH+7Khxk7rBK1bCXAuLLG51GFRTEPxju5k6aDdVOzr9eshTSce/xJh5rw==}}"
  FPY="$ROOT/bin/python/bin/python3"
  [ -x "$FPY" ] || FPY="$(command -v python3)"
  HAVE="$("$FPY" -I -c "import gzip,json,sys; print(json.load(gzip.open(sys.argv[1]))['version'])" "$OUT/foundry/foundry.json.gz" 2>/dev/null || true)"
  if [ "$HAVE" = "$FPEP_VERSION" ] && [ -z "${FORCE:-}" ]; then
    echo "data/foundry already built from warframe-public-export-plus $FPEP_VERSION"
    exit 0
  fi
  FWORK="$(mktemp -d)"
  trap 'rm -rf "$FWORK"' EXIT
  curl -sSfL -o "$FWORK/pep.tgz" "https://registry.npmjs.org/warframe-public-export-plus/-/warframe-public-export-plus-$FPEP_VERSION.tgz"
  if [ -n "$FPEP_SHA512" ]; then
    GOT="$(openssl dgst -sha512 -binary "$FWORK/pep.tgz" | base64 -w0)"
    [ "$GOT" = "$FPEP_SHA512" ] || { echo "integrity mismatch for warframe-public-export-plus $FPEP_VERSION" >&2; exit 1; }
  fi
  mkdir -p "$FWORK/pkg"
  tar -xzf "$FWORK/pep.tgz" -C "$FWORK/pkg" package/ExportRecipes.json package/ExportResources.json \
    package/ExportWarframes.json package/ExportWeapons.json package/ExportSentinels.json package/ExportUpgrades.json \
    package/ExportArcanes.json package/ExportCustoms.json package/ExportGear.json package/ExportKeys.json \
    package/ExportDrones.json package/dict.en.json package/package.json
  PYTHONPATH="$ROOT/py_modules" "$FPY" -m wfh.inventory_view build "$FWORK/pkg/package" "$OUT/foundry"
)

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
