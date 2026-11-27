#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行跑一次 AI 签到（给 Windows 计划任务用：不开窗口、不依赖网页）。

用法：
    python -m modules.ai_checkin.runner            # 跑所有启用的平台
    python -m modules.ai_checkin.runner --dry      # 只查状态，不领取
    python -m modules.ai_checkin.runner --site trae --site workbuddy
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from modules.ai_checkin import core


def main() -> int:
    parser = argparse.ArgumentParser(description="AI 工具签到（命令行）")
    parser.add_argument("--dry", action="store_true", help="只查状态，不领取")
    parser.add_argument("--site", action="append", help="只跑指定平台 id，可重复")
    args = parser.parse_args()

    cfg = core.load_config()
    valid = {p["id"] for p in core.PLATFORMS}
    targets = [t for t in (args.site or core.enabled_platforms(cfg)) if t in valid]
    if not targets:
        print("没有启用的平台，跳过")
        return 0

    core._append_log(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} 命令行运行：{'、'.join(targets)} =====")
    print(f"开始签到：{'、'.join(targets)}")

    if cfg.get("keepalive") and not args.dry:
        for line in core.keepalive():
            core._append_log(f"[续期] {line}")
            print(f"[续期] {line}")

    summary = {"ok": 0, "fail": 0, "skip": 0}
    results: dict[str, dict] = {}
    for key in targets:
        name = next((p["name"] for p in core.PLATFORMS if p["id"] == key), key)
        try:
            result = core.run_platform(key, cfg, dry=args.dry)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "status": "failed", "message": f"{type(exc).__name__}: {exc}"}
        if result["status"] == "skipped":
            summary["skip"] += 1
        elif result["ok"]:
            summary["ok"] += 1
        else:
            summary["fail"] += 1
        results[key] = {"status": result["status"], "message": result["message"],
                        "at": time.strftime("%Y-%m-%d %H:%M")}
        core._append_log(f"[{name}] {result['status']}：{result['message']}")
        print(f"[{name}] {result['status']}：{result['message']}")

    if not args.dry:
        state = core.load_state()
        state["results"].update(results)
        state["last_run_date"] = time.strftime("%Y-%m-%d")
        core.save_state(state)

    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())