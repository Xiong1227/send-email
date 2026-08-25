#!/usr/bin/env python3
"""配置 Windows 任务计划程序，定时运行 daily_news.py"""
import subprocess
import sys
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).parent
PYTHON = SCRIPT_DIR / "venv" / "Scripts" / "pythonw.exe"  # pythonw = 无控制台窗口
SCRIPT = SCRIPT_DIR / "daily_news.py"

# 任务参数
TASK_NAME = "DailyNewsEmail"
# 默认每天 08:00 和 18:00 各一次
SCHEDULE = sys.argv[1] if len(sys.argv) > 1 else "DAILY_8_18"

schedule_xml = {
    "DAILY_8_18": """
        <Triggers>
            <CalendarTrigger>
                <StartBoundary>2026-06-26T08:00:00</StartBoundary>
                <Repetition>
                    <Interval>PT12H</Interval>
                    <Duration>P1D</Duration>
                </Repetition>
                <Enabled>true</Enabled>
            </CalendarTrigger>
        </Triggers>""",
    "DAILY_8": """
        <Triggers>
            <CalendarTrigger>
                <StartBoundary>2026-06-26T08:00:00</StartBoundary>
                <ScheduleByDay>
                    <DaysInterval>1</DaysInterval>
                </ScheduleByDay>
                <Enabled>true</Enabled>
            </CalendarTrigger>
        </Triggers>""",
    "EVERY_4H": """
        <Triggers>
            <CalendarTrigger>
                <StartBoundary>2026-06-26T00:00:00</StartBoundary>
                <Repetition>
                    <Interval>PT4H</Interval>
                    <Duration>P1D</Duration>
                </Repetition>
                <Enabled>true</Enabled>
            </CalendarTrigger>
        </Triggers>""",
}.get(SCHEDULE, "")

# 用 PowerShell 创建任务
ps_script = rf'''
$action = New-ScheduledTaskAction -Execute "{PYTHON}" -Argument "{SCRIPT}" -WorkingDirectory "{SCRIPT_DIR}"
$trigger = New-ScheduledTaskTrigger -Daily -At "08:00"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RunOnlyIfNetworkAvailable
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive -RunLevel Limited

# 添加第二个触发器（18:00）
$trigger2 = New-ScheduledTaskTrigger -Daily -At "18:00"

Register-ScheduledTask -TaskName "{TASK_NAME}" -Action $action -Trigger $trigger,$trigger2 -Settings $settings -Principal $principal -Description "每日新闻简报自动发送" -Force

# 立即运行一次测试
Start-ScheduledTask -TaskName "{TASK_NAME}"
Write-Host "✅ 任务已创建: {TASK_NAME}"
Write-Host "   运行时间: 每天 08:00 & 18:00"
Write-Host "   任务计划程序 → 任务计划程序库 → {TASK_NAME} 可查看/修改"
'''

print("📅 配置 Windows 定时任务...")
print(f"   Python: {PYTHON}")
print(f"   脚本:   {SCRIPT}")
print()

result = subprocess.run(
    ["powershell", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
    capture_output=True, text=True, encoding="utf-8", errors="replace"
)

print(result.stdout)
if result.returncode != 0:
    print("❌ 错误:", result.stderr)
    print()
    print("👇 手动创建方法：")
    print("1. Win+R → taskschd.msc")
    print("2. 右侧「创建任务」→ 名称: DailyNewsEmail")
    print(f"3. 操作 → 新建 → 程序: {PYTHON}")
    print(f"4. 参数: {SCRIPT}")
    print(f"5. 起始于: {SCRIPT_DIR}")
    print("6. 触发器 → 新建 → 每天 08:00 + 每天 18:00")
else:
    print("🎉 配置完成！")
    print()
    print("📋 管理：")
    print("   taskschd.msc  → 任务计划程序库 → DailyNewsEmail")
    print("   右键任务 → 运行/禁用/删除")
