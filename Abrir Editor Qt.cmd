@echo off
setlocal
cd /d "%~dp0"
if defined GXT_QT_PYTHON (
  "%GXT_QT_PYTHON%" "%~dp0gxt_editor_qt.py"
) else if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0gxt_editor_qt.py"
) else (
  python "%~dp0gxt_editor_qt.py"
)
if errorlevel 1 (
  echo.
  echo Nao foi possivel iniciar. Veja a instalacao no README.md.
  pause
)
