@echo off
setlocal
cd /d "%~dp0\.."
set "PYTHONPATH=%CD%\src"
python -m gameko.gui
