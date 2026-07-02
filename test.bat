@echo off
REM Isha Life Ordering - run the test suite on Windows. Mirrors test.sh.
REM Tests ALWAYS use a disposable SQLite database (forced by tests/conftest.py)
REM and never touch Postgres.
setlocal
cd /d "%~dp0backend"

where py >nul 2>&1 && (set "PY=py") || (set "PY=python")
if not exist ".venv" ( %PY% -m venv .venv )
call ".venv\Scripts\activate.bat"
python -m pip install -q -r requirements.txt

REM Do NOT let tests inherit a real database URL.
set "DATABASE_URL="
python -m pytest -q tests\ %*
endlocal
