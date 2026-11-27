"""淘宝买家侧聊天导出；Python Playwright 控制浏览器，DOM 脚本只在页面内运行。"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parent
HOME = Path(os.environ.get("TAOBAO_CHAT_HOME") or ROOT.parents[3] / "modules" / "taobao_chat" / "data")
OUT = Path(os.environ.get("TAOBAO_CHAT_OUT") or HOME / "export")
PROFILE = Path(os.environ.get("TAOBAO_CHAT_PROFILE") or HOME / "profile")
CHAT_ENTRY = "https://market.m.taobao.com/app/im/chat/index.html?type=web"
LOGIN_URL = "https://login.taobao.com/member/login.jhtml?redirectURL=" + quote("https://www.taobao.com/", safe="")
FRAME_JS = (ROOT / "extract_dom.js").read_text(encoding="utf-8")


def log(message):
    print(message, flush=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="淘宝聊天导出（Python Playwright）")
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--logout", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    parser.add_argument("--input", default="")
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("urls", nargs="*")
    return parser.parse_args(argv)


def chrome_path():
    candidates = [os.environ.get("TAOBAO_CHROME_PATH"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]
    return next((str(p) for p in candidates if p and Path(p).is_file()), None)


def launch(playwright, headless):
    PROFILE.mkdir(parents=True, exist_ok=True)
    options = {"headless": headless, "viewport": {"width": 1280, "height": 880},
               "locale": "zh-CN", "args": ["--disable-blink-features=AutomationControlled"]}
    chrome = chrome_path()
    if chrome:
        options["executable_path"] = chrome
    context = playwright.chromium.launch_persistent_context(str(PROFILE), **options)
    context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    return context


def login_cookies(context):
    # 游客也有 cookie2/_tb_token_，只有 unb 能标识已登录账号。
    unb = next((c.get("value", "") for c in context.cookies()
                if c.get("name") == "unb" and re.search(r"(^|\.)taobao\.com$", c.get("domain", ""))), "")
    return {"ok": bool(unb), "uid": str(unb), "name": ""}


def wait_for_login(context, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = login_cookies(context)
        if info["ok"]:
            return info
        time.sleep(1.5)
    return {"ok": False, "uid": "", "name": ""}


def goto(page, url):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass  # 页面可能保持长连接；后续用实际 DOM 检查是否就绪。


def wait_for_frame(page, selector, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for frame in page.frames:
            try:
                if frame.locator(selector).count():
                    return frame
            except Exception:
                continue
        time.sleep(1)
    return None


def uid_from_url(url):
    try:
        return re.sub(r"^cntaobao", "", parse_qs(urlparse(url).query).get("uid", [""])[0])
    except ValueError:
        return ""


def to_viewer_message(raw, context):
    date = raw.get("date") or context.get("lastDate") or datetime.now().strftime("%Y-%m-%d")
    if raw.get("date"):
        context["lastDate"] = date
    stamp = 0
    if raw.get("time"):
        try:
            stamp = int(datetime.fromisoformat(f"{date} {raw['time']}").timestamp() * 1000)
        except ValueError:
            pass
    if not stamp:
        stamp = context.get("lastTs", 0) + 1 if context.get("lastTs") else int(time.time() * 1000)
    context["lastTs"] = stamp
    audio, video, kept = "", "", []
    for line in str(raw.get("text") or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("[视频] "):
            video = stripped[5:].strip()
        elif stripped.startswith("[语音] "):
            audio = stripped[5:].strip()
        else:
            kept.append(line)
    mine = bool(raw.get("self"))
    return {"from": "me" if mine else context["convId"],
            "fromName": raw.get("sender") or ("我" if mine else context["partner"]),
            "avatar": "" if mine else context.get("avatar", ""), "text": "\n".join(kept).strip(),
            "ts": stamp, "pics": [raw["imgUrl"]] if raw.get("imgUrl") else [],
            "audio": audio, "audioRaw": audio, "video": video, "recall": False}


def message_key(message):
    # 保留原始媒体地址作去重键，下载为本地路径后增量导出仍能合并。
    return (message.get("ts", 0), message.get("from", ""), message.get("text", ""),
            tuple(message.get("sourcePics", message.get("pics", []))),
            message.get("audioRaw", message.get("audio", "")),
            message.get("videoRaw", message.get("video", "")))


def merge_conv(conversations, conversation):
    existing = next((c for c in conversations if c["id"] == conversation["id"]), None)
    if existing is None:
        existing = next((c for c in conversations if c["name"] == conversation["name"]), None)
    if existing is None:
        conversations.append(conversation)
        return conversation
    seen = {message_key(m) for m in existing["messages"]}
    for message in conversation["messages"]:
        key = message_key(message)
        if key not in seen:
            existing["messages"].append(message)
            seen.add(key)
    existing["messages"].sort(key=lambda m: m.get("ts", 0))
    existing["count"] = len(existing["messages"])
    if not existing.get("avatar"):
        existing["avatar"] = conversation.get("avatar", "")
    return existing


def render_viewer(data):
    OUT.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    (OUT / "淘宝聊天记录.html").write_text((ROOT / "template.html").read_text(encoding="utf-8").replace("__DATA__", payload), encoding="utf-8")


def localize_media(context, conversations):
    cache = {}

    def download(url, folder):
        if not url or not str(url).startswith(("http://", "https://", "//")):
            return url
        if url in cache:
            return cache[url]
        remote = "https:" + url if url.startswith("//") else url
        digest = hashlib.sha256(remote.encode()).hexdigest()[:24]
        suffix = Path(urlparse(remote).path).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
            suffix = ""
        directory = OUT / folder
        directory.mkdir(parents=True, exist_ok=True)
        existing = next(directory.glob(digest + ".*"), None)
        if existing:
            result = existing.relative_to(OUT).as_posix()
            cache[url] = result
            return result
        try:
            response = context.request.get(remote, timeout=30000)
            if not response.ok:
                raise RuntimeError(f"HTTP {response.status}")
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            suffix = suffix or mimetypes.guess_extension(content_type) or ".bin"
            target = directory / (digest + suffix)
            target.write_bytes(response.body())
            result = target.relative_to(OUT).as_posix()
            cache[url] = result
            return result
        except Exception as exc:
            log(f"  [!] 媒体下载失败，保留在线链接：{type(exc).__name__}")
            cache[url] = url
            return url

    for conversation in conversations:
        conversation["avatar"] = download(conversation.get("avatar", ""), "avatars")
        for message in conversation.get("messages", []):
            message["avatar"] = download(message.get("avatar", ""), "avatars")
            message.setdefault("sourcePics", list(message.get("pics", [])))
            message["pics"] = [download(url, "images") for url in message.get("pics", [])]
            message.setdefault("audioRaw", message.get("audio", ""))
            message.setdefault("videoRaw", message.get("video", ""))
            message["audio"] = download(message.get("audio", ""), "audio")
            message["video"] = download(message.get("video", ""), "video")


def discover_conversations(frame):
    seen = {}
    for _ in range(100):
        items = frame.evaluate("""() => [...document.querySelectorAll('.conversation-item')].map(el=>({rawId:el.id||'',name:(el.querySelector('.name')?.innerText||'').trim(),avatar:el.querySelector('.avatar-img')?.getAttribute('src')||''})).filter(x=>x.name)""")
        added = 0
        for item in items:
            if item["name"] not in seen:
                seen[item["name"]] = item
                added += 1
        moved = frame.evaluate(SCROLL_JS)
        time.sleep(.35)
        if not added and moved.get("atBottom", True):
            break
    return list(seen.values())


SCROLL_JS = """() => {const list=document.querySelector('.ww_conversation_list')||document.querySelector('.ww_conversation');if(!list)return {moved:0,atBottom:true};let sc=null,best=0;for(const node of [list,...list.querySelectorAll('*')]){const over=node.scrollHeight-node.clientHeight;if(/(auto|scroll)/.test(getComputedStyle(node).overflowY)&&over>best){sc=node;best=over}}sc=sc||list;const before=sc.scrollTop;sc.scrollTop+=Math.max(100,sc.clientHeight*.6);return {moved:sc.scrollTop-before,atBottom:sc.scrollTop+sc.clientHeight>=sc.scrollHeight-2}}"""


def open_conversation(frame, conversation):
    search = frame.locator('input[placeholder*="搜索"]').first
    used = False
    try:
        if search.count():
            search.fill(conversation["name"])
            time.sleep(.6)
            used = True
        item = frame.locator(".conversation-item").filter(has_text=conversation["name"]).first
        if not item.count():
            if used:
                search.fill("")
            frame.evaluate("""() => {const list=document.querySelector('.ww_conversation_list')||document.querySelector('.ww_conversation');if(list)for(const node of [list,...list.querySelectorAll('*')])if(/(auto|scroll)/.test(getComputedStyle(node).overflowY))node.scrollTop=0}""")
            for _ in range(40):
                time.sleep(.3)
                if item.count():
                    break
                if not frame.evaluate(SCROLL_JS).get("moved"):
                    break
        if not item.count():
            return False
        item.click()
        time.sleep(1.4)
        return True
    finally:
        if used:
            try:
                search.fill("")
            except Exception:
                pass


def add_extracted(conversations, result, stable, name="", avatar=""):
    rows = result.get("messages", [])
    if not rows:
        raise RuntimeError("没有解析到消息")
    conv_id = "tb:" + stable
    context = {"convId": conv_id, "partner": name or result.get("partner") or "淘宝卖家",
               "avatar": result.get("avatar") or avatar}
    messages = [to_viewer_message(row, context) for row in rows]
    messages.sort(key=lambda m: m["ts"])
    merged = merge_conv(conversations, {"id": conv_id, "name": context["partner"], "avatar": context["avatar"],
                                       "isGroup": False, "count": len(messages), "messages": messages})
    log(f"  [+] 已合并进「{merged['name']}」，当前共 {merged['count']} 条消息")


def export_chats(context, options):
    urls = options.url + options.urls
    if options.input:
        payload = json.loads(Path(options.input).read_text(encoding="utf-8-sig"))
        urls.extend(payload.get("urls", []))
    urls = list(dict.fromkeys(str(url).strip() for url in urls if str(url).strip()))
    info = login_cookies(context)
    page = context.pages[0] if context.pages else context.new_page()
    if not info["ok"]:
        if options.headless or os.environ.get("TAOBAO_HEADLESS") == "1":
            raise RuntimeError("未检测到淘宝登录态，请先登录淘宝再导出")
        goto(page, LOGIN_URL)
        log("[*] 请在浏览器中登录淘宝…")
        info = wait_for_login(context)
        if not info["ok"]:
            raise RuntimeError("登录超时，请重新登录后导出")
    previous = None
    if options.mode != "full":
        try:
            previous = json.loads((OUT / "raw.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    conversations = previous.get("convs", []) if isinstance(previous, dict) else []
    exported = 0
    if options.auto or not urls:
        goto(page, CHAT_ENTRY)
        frame = wait_for_frame(page, ".conversation-item")
        if frame is None:
            raise RuntimeError("没有找到淘宝会话列表，可能未登录或页面结构变化")
        items = [{"name": options.only, "rawId": ""}] if options.only else discover_conversations(frame)
        for index, item in enumerate(items, 1):
            log(f"[{index}/{len(items)}] {item['name']}")
            try:
                if not open_conversation(frame, item):
                    raise RuntimeError("会话列表中未找到这一项")
                frame.wait_for_selector(".message-list", timeout=15000)
                result = frame.evaluate(FRAME_JS)
                match = re.search(r"-(\d+)\.\d+#", item.get("rawId", ""))
                stable = match.group(1) if match else hashlib.md5((item.get("rawId") or item["name"]).encode()).hexdigest()[:12]
                add_extracted(conversations, result, stable, item["name"], item.get("avatar", ""))
                exported += 1
            except Exception as exc:
                log(f"  [!] 导出失败：{exc}")
    else:
        for index, url in enumerate(urls, 1):
            log(f"[{index}/{len(urls)}] 开始导出聊天链接")
            try:
                goto(page, url)
                frame = wait_for_frame(page, ".message-list")
                if frame is None:
                    if not login_cookies(context)["ok"] and not (options.headless or os.environ.get("TAOBAO_HEADLESS") == "1"):
                        log("[*] 页面要求重新登录，等待登录…")
                        if wait_for_login(context, 180)["ok"]:
                            goto(page, url)
                            frame = wait_for_frame(page, ".message-list", 20)
                    if frame is None:
                        raise RuntimeError("没找到聊天窗口，请确认链接属于已登录账号的具体聊天")
                result = frame.evaluate(FRAME_JS)
                stable = uid_from_url(url) or hashlib.md5((result.get("partner") or url).encode()).hexdigest()[:12]
                add_extracted(conversations, result, stable)
                exported += 1
            except Exception as exc:
                log(f"  [!] 导出失败：{exc}")
    # 增量模式不能仅因为旧文件存在，就把本次全部失败报告成成功。
    if not exported:
        raise RuntimeError("本次没有成功导出任何会话，原有导出文件未更改")
    conversations.sort(key=lambda c: max((m.get("ts", 0) for m in c.get("messages", [])), default=0), reverse=True)
    localize_media(context, conversations)
    data = {"exportedAt": datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
            "me": previous.get("me", {"uid": "me", "name": "我", "avatar": ""}) if previous else {"uid": "me", "name": "我", "avatar": ""},
            "convs": conversations, "total": sum(len(c.get("messages", [])) for c in conversations)}
    OUT.mkdir(parents=True, exist_ok=True)
    raw = OUT / "raw.json"
    tmp = raw.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, raw)
    render_viewer(data)
    log(f"✅ 导出完成：{len(conversations)} 个会话，共 {data['total']} 条消息")


def main(argv=None):
    options = parse_args(argv)
    if options.logout:
        import shutil
        shutil.rmtree(PROFILE, ignore_errors=True)
        return 0
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            context = launch(playwright, options.status or (options.headless and not options.login) or (os.environ.get("TAOBAO_HEADLESS") == "1" and not options.login))
            try:
                if options.status:
                    log(json.dumps(login_cookies(context), ensure_ascii=False))
                elif options.login:
                    info = login_cookies(context)
                    if not info["ok"]:
                        page = context.pages[0] if context.pages else context.new_page()
                        goto(page, LOGIN_URL)
                        log("[*] 请在浏览器中登录淘宝…")
                        info = wait_for_login(context)
                    if not info["ok"]:
                        raise RuntimeError("登录超时（5 分钟）")
                    time.sleep(3)
                    log("✅ 登录成功，登录态已保存")
                else:
                    export_chats(context, options)
            finally:
                context.close()
        return 0
    except Exception as exc:
        log(f"[-] {exc}")
        return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    raise SystemExit(main())
