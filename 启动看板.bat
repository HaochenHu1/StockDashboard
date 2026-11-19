@echo off
rem Start the dashboard connected to the local OpenD, then open http://127.0.0.1:8600
cd /d "%~dp0"
start "" /min cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8600"
.venv\Scripts\python.exe -m backend
pause
