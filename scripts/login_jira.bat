@echo off
setlocal
cd /d "%~dp0\.."
wscript.exe //B //Nologo "%~dp0run_login_silent.vbs" jira
exit /b 0
