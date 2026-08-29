@echo off
REM 每日新闻简报 - 手动运行入口（工作目录跟随本脚本所在位置）
cd /d "%~dp0"

if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" daily_news.py %*
) else (
    echo [提示] 未找到 venv\Scripts\python.exe，改用系统 Python。建议先: python -m venv venv ^& venv\Scripts\pip install -r requirements.txt
    python daily_news.py %*
)

pause
