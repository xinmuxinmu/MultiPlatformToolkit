"""StarSky Python 命令行入口，也用于 Windows 每日计划任务。"""
from pathlib import Path
import argparse
import os
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def main(argv=None):
    parser = argparse.ArgumentParser(description="StarSky Python 签到")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--home")
    parser.add_argument("--schedule", metavar="HH:MM")
    parser.add_argument("--remove-schedule", action="store_true")
    options = parser.parse_args(argv)
    if options.home:
        os.environ["STARSKY_HOME"] = options.home
    from modules.starsky import core
    if options.schedule or options.remove_schedule:
        if os.name != "nt":
            print("仅支持 Windows")
            return 2
        args = core.schedule_args(options.schedule) if options.schedule else ["/Delete", "/TN", core.TASK_NAME, "/F"]
        if core.is_admin():
            return subprocess.run(["schtasks.exe", *args]).returncode
        code, message = core.run_elevated("schtasks.exe", args)
        if message:
            print(message)
        return code
    return core.run_checkin(options.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
