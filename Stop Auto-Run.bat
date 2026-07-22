@echo off
schtasks /delete /tn "TwinLeadPulse-AutoUpdate" /f
echo Auto-run stopped.
pause
