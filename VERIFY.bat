@echo off
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
echo Verifying (takes a few minutes: it re-reads the live year indexes)...
echo Add --fix to also download whatever is missing:  VERIFY.bat --fix
echo.
python elcinema_scraper.py verify --data-dir elcinema_data %* > verify_report.txt 2>&1
type verify_report.txt
echo.
echo Report saved in verify_report.txt
pause
