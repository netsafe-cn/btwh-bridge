@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Bosch Water Heater Bridge

rem 用 PATH 里的 python；虚拟环境请先 activate
set PY=python
where python >nul 2>nul || set PY=py

echo ============================================
echo   Bosch Therm -^> Bemfa Cloud Bridge
echo   Close this window to stop
echo ============================================
echo.
"%PY%" bosch_bridge.py --gap 10
echo.
echo Bridge stopped.
pause
