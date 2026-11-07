@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Stop all Bosch heater processes
echo ============================================
echo.

powershell -NoProfile -Command ^
  "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*bosch_*' }; if ($p) { $p | ForEach-Object { Write-Host ('  stopping PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force } } else { Write-Host '  no running instance' }"

echo.
echo Done. The Bluetooth link takes 10-15s to fully release.
echo Wait a moment before starting again, otherwise it may fail to connect.
echo.
ping -n 7 127.0.0.1 >nul
