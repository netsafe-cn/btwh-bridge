@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import tkinter" 2>nul
if errorlevel 1 (
    echo ============================================================
    echo  当前 Python 未安装 tkinter，图形面板不可用。
    echo  已自动切换到终端模式：在本窗口直接输入 6 位面板码并回车。
    echo  日志会自动保存到 esp32\logs\ 目录。
    echo ============================================================
    echo.
    python gateway_cli.py
) else (
    python esp32_panel.py
)
pause
