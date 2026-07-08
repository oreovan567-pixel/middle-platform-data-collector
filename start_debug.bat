@echo off
chcp 65001 >nul
title 数据采集系统 [开发模式]
cd /d "%~dp0"
call venv\Scripts\activate.bat
python main.py --debug
pause
