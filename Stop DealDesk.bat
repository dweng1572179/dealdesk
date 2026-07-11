@echo off
taskkill /f /im uvicorn.exe >nul 2>nul
echo DealDesk stopped. You can close this window.
pause
