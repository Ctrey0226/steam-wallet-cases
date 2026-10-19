@echo off
chcp 65001 >nul
python "%~dp0steam_cases.py" fetch
pause
