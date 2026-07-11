@echo off
rem Double-click to start DealDesk. No Docker needed - just Python.
cd /d "%~dp0"
if not exist .env copy .env.example .env >nul
where python >nul 2>nul || (
  echo Python is not installed.
  echo Get it from https://www.python.org/downloads/ - tick "Add python.exe to PATH" - then double-click this file again.
  pause & exit /b 1
)
if not exist .venv\Scripts\uvicorn.exe (
  echo First run - installing DealDesk. This takes a couple of minutes...
  python -m venv .venv && .venv\Scripts\pip install -q -r requirements.txt || (echo Install failed. & pause & exit /b 1)
)
if not defined DEALDESK_PORT set DEALDESK_PORT=8799
echo Starting DealDesk at http://localhost:%DEALDESK_PORT%
start "DealDesk - leave this window open" .venv\Scripts\uvicorn.exe app.app:app --host 127.0.0.1 --port %DEALDESK_PORT%
timeout /t 8 >nul
start "" http://localhost:%DEALDESK_PORT%
