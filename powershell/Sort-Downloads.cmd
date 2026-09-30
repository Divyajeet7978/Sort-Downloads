@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Sort-Downloads.ps1" %*
pause
