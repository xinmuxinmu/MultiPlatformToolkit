from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def parser(platform: str) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=f"{platform} 聊天记录 Python 导出器")
    actions = result.add_mutually_exclusive_group()
    actions.add_argument("--login", action="store_true")
    actions.add_argument("--status", action="store_true")
    actions.add_argument("--logout", action="store_true")
    result.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    result.add_argument("--max-dm", type=int, default=100000)
    result.add_argument("--max-group", type=int, default=3000)
    result.add_argument("--only-uid", default="")
    return result


def browser_options() -> dict:
    """Prefer an installed browser; the Playwright Chromium installation is also supported."""
    candidates = [os.environ.get("CHAT_BROWSER_PATH", "")]
    for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        if root:
            candidates.extend(str(Path(root) / relative) for relative in (
                "Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"))
    candidates.extend(filter(None, (shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("msedge"))))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return {"executable_path": candidate}
    return {}


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def read_baseline(out: Path) -> dict:
    path = out / "raw.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise RuntimeError("旧 raw.json 无法读取；为保护历史记录，本次停止导出") from exc
    if not isinstance(value, dict) or not isinstance(value.get("convs"), list):
        raise RuntimeError("旧 raw.json 结构不合法；为保护历史记录，本次停止导出")
    return value


def message_key(message: dict):
    return (message.get("ts") or 0, str(message.get("id") or ""))


def merge_messages(new: list[dict], old: list[dict] | None = None) -> list[dict]:
    by_id = {str(item.get("id")): dict(item) for item in (old or [])}
    by_id.update({str(item.get("id")): item for item in new})
    return sorted(by_id.values(), key=message_key)


def finish(out: Path, template: Path, filename: str, me: dict, convs: list[dict]) -> dict:
    for conv in convs:
        conv["messages"] = merge_messages(conv.get("messages") or [])
        conv["count"] = len(conv["messages"])
    convs.sort(key=lambda item: (bool(item["count"]), item["messages"][-1].get("ts", 0) if item["count"] else 0), reverse=True)
    payload = {"exportedAt": datetime.now(timezone.utc).isoformat(), "me": me,
               "fixed": True, "total": sum(item["count"] for item in convs), "convs": convs}
    # Render before replacing the raw baseline, so missing/broken templates cannot discard history.
    source = template.read_text(encoding="utf-8")
    if "__DATA__" not in source:
        raise RuntimeError("离线页面模板缺少 __DATA__ 标记")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    page = out / filename
    temporary = page.with_name(page.name + ".tmp")
    temporary.write_text(source.replace("__DATA__", encoded), encoding="utf-8")
    write_json(out / "raw.json", payload)
    os.replace(temporary, page)
    print(f"\n导出完成：{len(convs)} 个会话，{payload['total']} 条消息", flush=True)
    print(f"文件位置：{out.resolve()}", flush=True)
    return payload


def safe_name(value: str) -> str:
    value = str(value)
    return value if re.fullmatch(r"[\w-]+", value, flags=re.ASCII) else hashlib.md5(value.encode()).hexdigest()


class MediaStore:
    def __init__(self, out: Path, request, headers: dict | None = None):
        self.out, self.request, self.headers = out, request, headers or {}
        self.cache: dict[str, str] = {}

    def existing(self, stem: str, extensions: tuple[str, ...]) -> str:
        for extension in extensions:
            relative = f"{stem}.{extension}"
            if (self.out / relative).is_file():
                return relative
        return ""

    def save(self, relative: str, body: bytes) -> str:
        target = self.out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(body)
        os.replace(temporary, target)
        return relative

    def download(self, url: str, stem: str, extensions=("jpg", "png", "gif", "webp"), timeout=20000) -> str:
        if not url or urlsplit(url).scheme not in ("http", "https"):
            return ""
        if url in self.cache:
            return self.cache[url]
        existing = self.existing(stem, extensions)
        if existing:
            self.cache[url] = existing
            return existing
        try:
            response = self.request.get(url, timeout=timeout, headers=self.headers)
            if response.status != 200:
                return ""
            content_type = response.headers.get("content-type", "").lower()
            if "text/html" in content_type or "application/json" in content_type:
                return ""
            extension = next((ext for ext in extensions if ext in content_type), extensions[0])
            result = self.save(f"{stem}.{extension}", response.body())
            self.cache[url] = result
            return result
        except Exception:
            return ""

    def image(self, url: str, folder="images") -> str:
        digest = hashlib.md5(url.split("?")[0].encode()).hexdigest()[:16]
        return self.download(url, f"{folder}/{digest}")


def convert_audio(store: MediaStore, relative: str) -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        for candidate in ("D:/Program/ffmpeg/bin/ffmpeg.exe", "C:/ffmpeg/bin/ffmpeg.exe"):
            if Path(candidate).is_file():
                ffmpeg = candidate
                break
    if not ffmpeg:
        return relative
    target = str(Path(relative).with_suffix(".mp3")).replace("\\", "/")
    try:
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(store.out / relative),
                        "-codec:a", "libmp3lame", "-q:a", "5", str(store.out / target)],
                       capture_output=True, check=True, timeout=90,
                       creationflags=0x08000000 if os.name == "nt" else 0)
        return target
    except (OSError, subprocess.SubprocessError):
        return relative


class BrowserSession:
    def __init__(self, chromium, prefix: str, tool: Path):
        self.chromium = chromium
        home = Path(os.environ.get(f"{prefix}_CHAT_HOME", str(tool.parent / "data")))
        self.out = Path(os.environ.get(f"{prefix}_CHAT_OUT", str(home / "export")))
        self.state = Path(os.environ.get(f"{prefix}_CHAT_STATE", str(home / "state.json")))
        self.profile = Path(os.environ.get(f"{prefix}_CHAT_PROFILE", str(home / "profile")))
        self.headless = os.environ.get(f"{prefix}_HEADLESS") != "0"
        self.browser = self.ctx = self.page = None

    def close(self):
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self.browser:
                self.browser.close()
            self.browser = self.ctx = self.page = None

    def saved(self, headless=True):
        self.browser = self.chromium.launch(headless=headless, **browser_options())
        self.ctx = self.browser.new_context(storage_state=str(self.state))
        self.page = self.ctx.new_page()

    def login(self, options, url: str, identity):
        if not options.login and self.state.exists():
            try:
                self.saved(self.headless)
                if identity():
                    print("登录态有效，开始导出…", flush=True)
                    return True
            except Exception:
                pass
            self.close()
        print("需要登录：请在打开的浏览器窗口中完成登录…", flush=True)
        self.ctx = self.chromium.launch_persistent_context(str(self.profile), headless=False,
            viewport={"width": 1200, "height": 820}, args=["--disable-blink-features=AutomationControlled"], **browser_options())
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
        except Exception:
            pass
        for _ in range(180):
            try:
                if identity():
                    write_json(self.state, self.ctx.storage_state())
                    print("登录成功，已保存登录态。", flush=True)
                    self.close()
                    if options.login:
                        print("LOGIN_DONE", flush=True)
                        return False
                    self.saved(self.headless)
                    return True
            except Exception:
                pass
            time.sleep(2)
        raise RuntimeError("登录超时，请重新登录")


def run_cli(platform: str, prefix: str, tool: Path, exporter, identity, login_url: str) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser(platform).parse_args()
    args.max_dm, args.max_group = max(20, args.max_dm), max(20, args.max_group)
    # Logout and a missing-state status do not require a browser installation.
    session = BrowserSession(None, prefix, tool)
    if args.logout:
        session.state.unlink(missing_ok=True)
        if session.profile.exists():
            shutil.rmtree(session.profile)
        print("已清除本地登录态。", flush=True)
        return 0
    result = {"ok": False, "name": "", "uid": ""}
    if args.status and not session.state.exists():
        print(json.dumps(result))
        return 0
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            session.chromium = playwright.chromium
            try:
                if args.status:
                    session.saved(True)
                    me = identity(session)
                    if me:
                        result.update(ok=True, **me)
                elif session.login(args, login_url, lambda: identity(session)):
                    session.out.mkdir(parents=True, exist_ok=True)
                    exporter(session, args, tool)
            finally:
                session.close()
    except Exception as exc:
        if not args.status:
            print(f"导出失败：{exc}", flush=True)
            return 1
        result["error"] = str(exc)
    if args.status:
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0
