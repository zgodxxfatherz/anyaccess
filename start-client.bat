@echo off
REM Start the VIEWER (the PC you control FROM). Launches a new window.
start "P2P Viewer" cmd /k python "%~dp0client.py"
echo Viewer starting... enter the host's address and press Connect.
