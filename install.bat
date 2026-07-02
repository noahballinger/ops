@echo off
REM Isha Life Ordering - one-time Windows setup.
REM Creates the Python environment, installs dependencies, and puts an
REM "Isha Ordering" shortcut on your Desktop that launches the app.
setlocal

echo ============================================
echo   Isha Life Ordering - Windows setup
echo ============================================
echo.

REM 1) Check Python is available.
where py >nul 2>&1 && (set "PY=py") || (set "PY=python")
%PY% --version >nul 2>&1 || (
  echo Python 3 was not found.
  echo Please install it from https://www.python.org/downloads/
  echo During install, TICK "Add python.exe to PATH", then re-run install.bat.
  echo.
  pause & exit /b 1
)

REM 2) Create the virtual environment and install dependencies.
cd /d "%~dp0backend"
if not exist ".venv" (
  echo Creating virtual environment ...
  %PY% -m venv .venv || (echo Could not create the virtual environment. & pause & exit /b 1)
)
call ".venv\Scripts\activate.bat"
echo Installing dependencies ^(this can take a minute^) ...
python -m pip install --upgrade pip -q
python -m pip install -q -r requirements.txt || (echo Dependency install failed. & pause & exit /b 1)

REM 3) Create a Desktop shortcut to run.bat.
echo Creating Desktop shortcut ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$w=New-Object -ComObject WScript.Shell; $lnk=$w.CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('Desktop'),'Isha Ordering.lnk')); $lnk.TargetPath='%~dp0run.bat'; $lnk.WorkingDirectory='%~dp0'; $lnk.IconLocation='%SystemRoot%\System32\shell32.dll,167'; $lnk.Description='Launch Isha Life Ordering'; $lnk.Save()"

echo.
echo ============================================
echo   Setup complete.
echo ============================================
echo  - An "Isha Ordering" shortcut is on your Desktop.
echo  - Before the first launch, start the database:
echo        docker compose up -d
echo    ^(requires Docker Desktop for Windows^)
echo  - Then double-click the shortcut, or run.bat.
echo.
pause
endlocal
