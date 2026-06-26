#!/usr/bin/env bash
# One-command launcher for the Isha Life USA Import Ordering tool.
# Creates a venv, installs deps, runs tests, then serves the app at :8000.
set -euo pipefail
ROOT="$(dirname "$0")"

# Load Odoo credentials / cache settings from a gitignored .env if present.
if [ -f "$ROOT/.env" ]; then
  echo "Loading $ROOT/.env"
  set -a; # shellcheck disable=SC1091
  source "$ROOT/.env"; set +a
fi

cd "$ROOT/backend"

PY=${PYTHON:-python3}
if [ ! -d ".venv" ]; then
  echo "Creating virtualenv ..."
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

echo "Running tests ..."
pytest -q tests/ || { echo "Tests failed"; exit 1; }

echo
echo "Starting server on http://localhost:8000  (Ctrl-C to stop)"
echo "Open that URL, create an order by uploading the workbook, review, export."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
