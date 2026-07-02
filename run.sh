#!/usr/bin/env bash
# One-command launcher for the Isha Life USA Import Ordering tool.
# Creates a venv, installs deps, then serves the app at :8000 (on Postgres).
# Tests run separately via ./test.sh so a launch never touches the database.
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

# The app runs on Postgres. Make sure it's up (docker compose up -d) and that
# DATABASE_URL points at it; the server fails loudly otherwise.
echo
echo "Starting server on http://localhost:8000  (Ctrl-C to stop)"
echo "Run the test suite separately with ./test.sh"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
