@echo off
chcp 65001 >nul
cd /d "%~dp0"
title BOSS 状态采集器

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 正在请求管理员权限...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

python boss_state_probe.py
echo.
pause
