@echo off
rem The iron command for Windows. Finds Python 3.8+ and runs Iron from this folder.
setlocal
set "IRON_PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "IRON_PY=py -3"
if not defined IRON_PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "IRON_PY=python"
if not defined IRON_PY (
  echo iron: Python 3.8 or newer is required: https://www.python.org/downloads/ 1>&2
  exit /b 1
)
%IRON_PY% "%~dp0iron_launch.py" cli %*
exit /b %ERRORLEVEL%
