@echo off
chcp 65001 > nul
title PTB Host Auth Helper (Port 8999)
echo ======================================================================
echo  PERSONAL TASK BOARD - HOST AUTH HELPER (PORT 8999)
echo ======================================================================
echo [*] Dang khoi chay Host Auth Helper tren cong 8999 tren may chinh...
echo [*] Dych vu nay giup Web UI (OpenWebUI :3000) mo trinh duyet Chromium/Edge
echo     truc tiep tren man hinh Windows cua ban de dang nhap Teams/Outlook.
echo ======================================================================
echo.

cd /d "%~dp0\.."
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe scripts\host_auth_helper.py
) else (
    python scripts\host_auth_helper.py
)
pause
