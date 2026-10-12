@echo off
REM Run the automated smoke test (no real input injection, no windows).
cd /d "%~dp0"
python smoke_test.py
pause
