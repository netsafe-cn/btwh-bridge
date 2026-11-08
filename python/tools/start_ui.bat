@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem use pythonw from PATH (no console window); fall back to python
set PY=pythonw
where pythonw >nul 2>nul || set PY=python

start "" "%PY%" bosch_ui.py
