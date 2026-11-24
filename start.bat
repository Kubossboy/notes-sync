@echo off
REM ATLAS launcher (Windows). Usage: start.bat setup^|test^|status^|loop^|dashboard^|stop^|kill "reason"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
