@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0apply_code_update.ps1" -PackageRoot "%~dp0."
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" (
  echo Code update completed. Personal data was preserved.
) else (
  echo Code update failed. No personal data was changed.
)
echo.
echo Press any key to close this window.
pause >nul
exit /b %EXIT_CODE%
