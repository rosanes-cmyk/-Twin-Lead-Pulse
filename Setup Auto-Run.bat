@echo off
cd /d "%~dp0"
schtasks /create /tn "TwinLeadPulse-AutoUpdate" /tr "\"%~dp0scheduled_update.bat\"" /sc minute /mo 30 /f
echo.
echo Auto-run is ON: pulls new Chat leads into the Sheet every 30 minutes.
echo Output is logged to run.log. To stop it, double-click "Stop Auto-Run.bat".
pause
