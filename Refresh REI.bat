@echo off
cd /d "%~dp0"
echo ===== Twin Lead Pulse: refreshing REI link/tags/status on all rows =====
python run.py --config config.json --enrich-sheet
echo.
echo Done. Check your Google Sheet.
pause
