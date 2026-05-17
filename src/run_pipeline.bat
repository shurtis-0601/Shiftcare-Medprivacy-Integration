@echo off
REM ============================================================
REM  ShiftCare MedPrivacy Pipeline — Windows Task Scheduler
REM
REM  Set PIPELINE_DIR to the folder where you cloned the repo.
REM  Set PYTHON to the full path to your python.exe (or "python"
REM  if it is already on PATH inside a virtual env activation).
REM ============================================================

set PIPELINE_DIR=C:\path\to\Shiftcare-Medprivacy-Integration
set PYTHON=%PIPELINE_DIR%\venv\Scripts\python.exe

cd /d "%PIPELINE_DIR%"
"%PYTHON%" run_local.py >> pipeline.log 2>&1
