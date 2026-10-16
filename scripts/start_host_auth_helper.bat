@echo off
setlocal
cd /d "%~dp0\.."
echo ======================================================================
echo  PERSONAL TASK BOARD - HOST AUTH HELPER (PORT 8999)
echo ======================================================================
echo [*] Khoi chay Host Auth Helper an trong background tren cong 8999...
wscript.exe //B //Nologo "%~dp0run_host_auth_helper_silent.vbs"
echo [✓] Da khoi chay ngam thanh cong! Cong 8999 san sang phuc vu.
timeout /t 2 > nul
exit /b 0
