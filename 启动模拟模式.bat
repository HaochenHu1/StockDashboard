@echo off
rem Start the dashboard in mock mode (no OpenD, simulated quotes, separate config.mock.yaml)
cd /d "%~dp0"
start "" /min cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8600"
.venv\Scripts\python.exe -m backend --mock
pause
