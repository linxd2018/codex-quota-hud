@echo off
setlocal
cd /d "%~dp0"
set "HUD_PY=%~dp0..\.build-venv\Scripts\python.exe"
if not exist "%HUD_PY%" (
  echo Create .build-venv in the project root and install source/requirements-build.txt first.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build_backdrop.ps1"
if errorlevel 1 exit /b 1
"%HUD_PY%" -m PyInstaller --noconfirm --distpath ..\release --workpath ..\build\pyinstaller CodexQuotaHUD.spec
if errorlevel 1 exit /b 1
echo Build complete: release\CodexQuotaHUD
pause
