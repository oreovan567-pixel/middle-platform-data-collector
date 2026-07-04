@echo off
chcp 65001 >nul
title 数据采集系统
echo.
echo   正在启动数据采集系统...
echo.
cd /d "%~dp0"
call venv\Scripts\activate.bat
python main.py
pause
