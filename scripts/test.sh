#!/usr/bin/env bash
# Runs the Python tests on the bundled runtime (pytest installed beside it in .devdeps).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/bin/python/bin/python3"
[ -x "$PY" ] || "$ROOT/scripts/build-runtime.sh"
if [ ! -d "$ROOT/.devdeps/pytest" ]; then
  if ! python3 -m pip --version >/dev/null 2>&1; then
    # No system pip (e.g. SteamOS): borrow pip from the cached CPython tarball.
    TARBALL="$(ls "${XDG_CACHE_HOME:-$HOME/.cache}"/wfh-decky/cpython-*.tar.gz 2>/dev/null | head -1)"
    [ -n "$TARBALL" ] || { echo "no pip available; run scripts/build-runtime.sh first" >&2; exit 1; }
    PIPPY="$ROOT/.devdeps/.pip-python"
    mkdir -p "$PIPPY" && tar -xzf "$TARBALL" -C "$PIPPY" --strip-components=1
    "$PIPPY/bin/python3" -m pip install --quiet --target "$ROOT/.devdeps" pytest
  else
    python3 -m pip install --quiet --target "$ROOT/.devdeps" pytest
  fi
fi
cd "$ROOT"
PYTHONPATH="$ROOT/py_modules:$ROOT/.devdeps" WFH_HEADLESS=1 "$PY" -m pytest -q "${@:-tests}"
