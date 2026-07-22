@echo off
cd /d "%~dp0"
echo ===== Twin Lead Pulse: pulling new leads from Google Chat + REI =====
python run.py --config config.json --from-chat
echo.
echo Done. Check your Google Sheet.
pause
