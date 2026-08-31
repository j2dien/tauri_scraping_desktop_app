@echo off
title Social Media Top Commenter Analyzer - Desktop App (Tauri v2)
cd /d %~dp0
set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
echo ========================================================
echo   Social Media Top Commenter Analyzer - Tauri Launcher
echo ========================================================
echo.
cd frontend
bun run desktop
pause
