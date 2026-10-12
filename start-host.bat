@echo off
REM Start the HOST (the PC you want to control). Launches a new window.
start "P2P Host" cmd /k python "%~dp0host.py"
echo Host starting... share the address shown in the host window with your viewer.
