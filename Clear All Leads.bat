@echo off
cd /d "%~dp0"
echo ===== Twin Lead Pulse: CLEAR all leads (fresh start) =====
python run.py --config config.json --clear-leads
echo.
pause
