@echo off
cd /d "%~dp0"
python run.py --config config.json --from-chat >> run.log 2>&1
