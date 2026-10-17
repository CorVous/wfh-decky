#!/usr/bin/env bash
# Runs the Python tests on the bundled runtime (pytest installed beside it in .devdeps).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/bin/python/bin/python3"
[ -x "$PY" ] || "$ROOT/scripts/build-runtime.sh"
if [ ! -d "$ROOT/.devdeps/pytest" ]; then
  python3 -m pip install --quiet --target "$ROOT/.devdeps" pytest
fi
cd "$ROOT"
PYTHONPATH="$ROOT/py_modules:$ROOT/.devdeps" WFH_HEADLESS=1 "$PY" -m pytest -q tests "$@"
