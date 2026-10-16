@echo off
chcp 65001 > nul
title PTB - Dang Nhap Microsoft Outlook Web
echo ======================================================================
echo  PERSONAL TASK BOARD - DANG NHAP MICROSOFT OUTLOOK VA TU DONG LUU COOKIE
echo ======================================================================
echo.
echo [*] Dang khoi chay Chromium tren man hinh...
echo [*] Vui long dang nhap tai khoan Microsoft Outlook va hoan thanh MFA tren trinh duyet.
echo [*] He thong se TU DONG PHAT HIEN va TU DONG LUU COOKIE vao:
echo     data\playwright\storage_state.json
echo.

cd /d "%~dp0\.."
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe -m ptb_acquisition.playwright.login --service outlook
) else (
    python -m ptb_acquisition.playwright.login --service outlook
)

echo.
echo ======================================================================
echo  HOAN TAT! Task Board tren OpenWebUI se tu dong chuyen sang HEALTHY.
echo ======================================================================
pause
