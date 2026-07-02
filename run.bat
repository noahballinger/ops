@echo off
REM Isha Life Ordering - Windows launcher (serve only). Mirrors run.sh.
REM Double-click this, or run it from a Command Prompt.
setlocal
cd /d "%~dp0backend"

REM Prefer the "py" launcher, fall back to "python".
where py >nul 2>&1 && (set "PY=py") || (set "PY=python")

if not exist ".venv" (
  echo Creating virtual environment ...
  %PY% -m venv .venv || (echo Could not create the virtual environment. Install Python 3 from https://www.python.org/downloads/ and check "Add python.exe to PATH". & pause & exit /b 1)
)

call ".venv\Scripts\activate.bat"
python -m pip install -q -r requirements.txt || (echo Dependency install failed. & pause & exit /b 1)

echo.
echo Isha Life Ordering is starting on http://localhost:8000
echo The app runs on Postgres - make sure it is up:  docker compose up -d
echo Run the tests separately with test.bat
echo Close this window or press Ctrl+C to stop.
echo.
start "" http://localhost:8000
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
endlocal
