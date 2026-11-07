@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 用 PATH 里的 pythonw（无控制台窗口）；找不到就退回 python
set PY=pythonw
where pythonw >nul 2>nul || set PY=python

start "" "%PY%" bosch_ui.py
