#!/usr/bin/env bash
# Run the test suite. Tests ALWAYS use a disposable SQLite database (forced by
# backend/tests/conftest.py) and never touch Postgres — see that file and the
# IS_SQLITE guard in the cached-sync tests.
set -euo pipefail
ROOT="$(dirname "$0")"
cd "$ROOT/backend"

PY=${PYTHON:-python3}
if [ ! -d ".venv" ]; then
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

# Deliberately do NOT source .env: tests must not inherit DATABASE_URL.
exec pytest -q tests/ "$@"
