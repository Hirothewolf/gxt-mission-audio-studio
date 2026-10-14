@echo off
setlocal
cd /d "%~dp0"
if defined GXT_CLASSIC_PYTHON (
  "%GXT_CLASSIC_PYTHON%" "%~dp0gxt_editor.py"
) else if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0gxt_editor.py"
) else (
  python "%~dp0gxt_editor.py"
)
if errorlevel 1 (
  echo.
  echo Nao foi possivel iniciar. Veja a instalacao no README.md.
  pause
)
