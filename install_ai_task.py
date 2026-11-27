"""Register a daily Python AI task without PowerShell or administrator rights."""
from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def task_xml(at="10:20", wake=False, python=None):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
        raise ValueError("时间格式应为 HH:MM，例如 10:20")
    executable = Path(python or sys.executable).resolve()
    pythonw = executable.with_name("pythonw.exe")
    if os.name == "nt" and pythonw.exists(): executable = pythonw
    ET.register_namespace("", NAMESPACE)
    def child(parent, tag, value=None, **attrs):
        node = ET.SubElement(parent, "{" + NAMESPACE + "}" + tag, attrs)
        if value is not None: node.text = value
        return node
    root = ET.Element("{" + NAMESPACE + "}Task", {"version": "1.2"})
    triggers = child(root, "Triggers")
    trigger = child(triggers, "CalendarTrigger")
    child(trigger, "StartBoundary", datetime.now().strftime("%Y-%m-%d") + "T" + at + ":00")
    child(trigger, "Enabled", "true")
    child(child(trigger, "ScheduleByDay"), "DaysInterval", "1")
    principal = child(child(root, "Principals"), "Principal", id="Author")
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    settings = child(root, "Settings")
    for key, value in {"MultipleInstancesPolicy": "IgnoreNew", "DisallowStartIfOnBatteries": "false",
                       "StopIfGoingOnBatteries": "false", "StartWhenAvailable": "true",
                       "Hidden": "true", "WakeToRun": str(wake).lower(),
                       "ExecutionTimeLimit": "PT30M"}.items(): child(settings, key, value)
    action = child(child(root, "Actions", Context="Author"), "Exec")
    child(action, "Command", str(executable))
    child(action, "Arguments", '-X utf8 -m modules.ai_checkin.runner')
    child(action, "WorkingDirectory", str(ROOT))
    return ET.tostring(root, encoding="unicode", xml_declaration=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description="安装/移除 Python AI 每日签到任务")
    parser.add_argument("--time", "-Time", default="10:20")
    parser.add_argument("--task-name", default="AI-AutoCheckin")
    parser.add_argument("--wake", action="store_true")
    parser.add_argument("--remove", "-Remove", action="store_true")
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("Windows 计划任务仅支持 Windows；其他系统可使用应用内定时。")
    if args.remove:
        return subprocess.call(["schtasks.exe", "/Delete", "/TN", args.task_name, "/F"])
    try: content = task_xml(args.time, args.wake)
    except ValueError as exc: parser.error(str(exc))
    handle, filename = tempfile.mkstemp(suffix=".xml", prefix="toolkit-task-")
    os.close(handle)
    try:
        Path(filename).write_text('<?xml version="1.0" encoding="UTF-16"?>\n' + content, encoding="utf-16")
        result = subprocess.call(["schtasks.exe", "/Create", "/TN", args.task_name, "/XML", filename, "/F"])
    finally:
        Path(filename).unlink(missing_ok=True)
    if result == 0: print(f"Python 每日签到任务已保存，时间 {args.time}。")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
