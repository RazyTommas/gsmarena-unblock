@echo off
cd /d "%~dp0"
echo == Firmware Watch ==
where python >nul 2>&1 && (set PY=python) || (where py >nul 2>&1 && set PY=py)
if "%PY%"=="" ( echo Python 3 not found. Install from https://python.org and re-run. & pause & exit /b 1 )
if not exist baseline_latest.csv ( echo baseline_latest.csv missing next to this file. & pause & exit /b 1 )
echo Launching dashboard on http://localhost:8900  (close this window to stop)
%PY% fw_dashboard.py %*
pause
