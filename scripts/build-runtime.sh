#!/usr/bin/env bash
# Builds the bundled Python runtime the worker runs on: a standalone CPython
# plus numpy, onnxruntime, Pillow and python-xlib, in bin/python.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/bin/python}"
PBS_TAG="20250604"
PY_VER="3.12.11"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-${PY_VER}+${PBS_TAG}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/wfh-decky"

mkdir -p "$CACHE"
TARBALL="$CACHE/cpython-${PY_VER}-${PBS_TAG}.tar.gz"
[ -f "$TARBALL" ] || curl -sSfL -o "$TARBALL" "$URL"

rm -rf "$OUT"
mkdir -p "$OUT"
tar -xzf "$TARBALL" -C "$OUT" --strip-components=1

"$OUT/bin/python3" -m pip install --quiet --no-cache-dir --disable-pip-version-check \
  --only-binary=:all: -r "$ROOT/requirements-runtime.txt"

# Trim what the worker never uses.
rm -rf "$OUT"/lib/python3.12/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3} \
  "$OUT"/lib/tcl* "$OUT"/lib/tk* "$OUT"/lib/itcl* "$OUT"/lib/thread* "$OUT"/include "$OUT"/share
find "$OUT" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$OUT"/lib/python3.12/site-packages -path '*/tests' -type d -prune -exec rm -rf {} +
rm -rf "$OUT"/lib/python3.12/site-packages/pip* "$OUT"/bin/pip*
du -sh "$OUT"
