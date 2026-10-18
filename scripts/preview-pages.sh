#!/usr/bin/env bash
# Dev-only: screenshot the full-screen pages and QAM sections without a Steam client.
#
#   scripts/preview-pages.sh                 all pages + all QAM sections
#   scripts/preview-pages.sh market relics   just those pages
#   scripts/preview-pages.sh qam:world qam   one QAM section / all QAM sections ("pages" = all pages)
#   scripts/preview-pages.sh --serve         build + start the bridge and print a URL to browse
#                                            (arrows = d-pad, Enter = A, Esc = B, Q/E = L1/R1)
#
# Builds preview/ with esbuild against mocks of @decky/ui and @decky/api, runs the real Python
# worker in-process behind preview/bridge.py (offline, fixture data, clock pinned to the
# recorded world state) and drives Playwright's chromium. Output: out/previews/<page>.png
# (full length), <page>-viewport.png (1280x760 under Steam's header), qam-<section>.png,
# report.json, bridge.log. Each run uses its own temp dir and free port, so copies can run in
# parallel (set WFH_PREVIEW_OUT to keep their screenshots apart). Never part of the plugin build
# or scripts/package.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${WFH_PREVIEW_OUT:-$ROOT/out/previews}"
PY="$ROOT/bin/python/bin/python3.12"
DEV="$ROOT/.devdeps"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/opt/pw-browsers}"

[ -x "$PY" ] || { echo "missing $PY (run scripts/build-runtime.sh)" >&2; exit 1; }
if [ ! -d "$DEV/node_modules/esbuild" ] || [ ! -d "$DEV/node_modules/react-dom" ]; then
  mkdir -p "$DEV"
  [ -f "$DEV/package.json" ] || echo '{"name":"wfh-preview-devdeps","private":true}' > "$DEV/package.json"
  (cd "$DEV" && npm install --no-audit --no-fund --silent esbuild@0.25.10 react@19.1.1 react-dom@19.1.1)
fi
if [ -z "${PLAYWRIGHT_MODULE:-}" ]; then
  for c in "$DEV/node_modules/playwright" "$(npm root -g 2>/dev/null)/playwright" /opt/node-tools/node_modules/playwright; do
    [ -d "$c" ] && { export PLAYWRIGHT_MODULE="$c"; break; }
  done
fi

SERVE=0
if [ "${1:-}" = "--serve" ]; then SERVE=1; shift; fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/wfh-preview.XXXXXX")"
BRIDGE_PID=""
cleanup() {
  [ -n "$BRIDGE_PID" ] && kill "$BRIDGE_PID" 2>/dev/null && wait "$BRIDGE_PID" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

node "$ROOT/preview/build.mjs" "$TMP/static" >/dev/null
"$PY" -s "$ROOT/preview/bridge.py" --tmp "$TMP/bridge" --static "$TMP/static" --port 0 >"$TMP/bridge.out" 2>"$TMP/bridge.err" &
BRIDGE_PID=$!
for _ in $(seq 1 300); do
  grep -q '^READY' "$TMP/bridge.out" 2>/dev/null && break
  kill -0 "$BRIDGE_PID" 2>/dev/null || { echo "bridge failed to start:" >&2; tail -40 "$TMP/bridge.err" >&2; exit 1; }
  sleep 0.2
done
grep -q '^READY' "$TMP/bridge.out" || { echo "bridge did not become ready" >&2; tail -40 "$TMP/bridge.err" >&2; exit 1; }
PORT="$(sed -n 's/^READY port=\([0-9]*\).*/\1/p' "$TMP/bridge.out")"
sed -n 's/^READY port=[0-9]* failed_modules=\(.*\)/bridge up; failed worker modules: \1/p' "$TMP/bridge.out"

if [ "$SERVE" = 1 ]; then
  echo "preview at http://127.0.0.1:$PORT/  (?page=<id> or ?qam=<section>); Ctrl-C to stop"
  wait "$BRIDGE_PID"
  exit 0
fi

mkdir -p "$OUT"
status=0
node "$ROOT/preview/shoot.mjs" --base "http://127.0.0.1:$PORT" --out "$OUT" "$@" || status=$?
cp "$TMP/bridge.err" "$OUT/bridge.log" 2>/dev/null || true
n_err=$(grep -c 'RPC_ERROR\|Traceback' "$TMP/bridge.err" || true)
echo "bridge log: $OUT/bridge.log ($n_err RPC error/traceback lines)"
exit $status
