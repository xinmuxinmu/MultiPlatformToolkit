"""Obtain Heybox's device token with the official integrity-pinned SDK."""
from __future__ import annotations

import hashlib
import re
import shutil
import urllib.request

from playwright.sync_api import sync_playwright

SDK_URL = "https://static.portal101.cn/dist/web/v3.0.0/fp.min.js"
SDK_SHA256 = "654b94891624c759ba1b887b39261d8e894144da5f56a6dc42b5b6e0d66562f9"
DEVICE_PROFILE_URL = "https://fp-it.portal101.cn/deviceprofile/v4"
TOKEN_RE = re.compile(r"B[A-Za-z0-9+/=_-]{88}")
DEVICE_TOKEN_CONFIG = {
    "organization": "0yD85BjYvGFAvHaSQ1mc",
    "appId": "heybox_website",
    "publicKey": (
        "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCXj9exmI4nQjmT52iwr+yf7hAQ06bfSZHTAHUfRBYiagCf/whhd8es0R79wBigpiHLd28TKA8b8mGR8OiiI1hV+qfynCWihvp3mdj8MiiH6SU3lhro2hkfYzImZB0RmWr2zE4Xt1+A6Oyp6bf+W7JSxYUXHw3nNv7Td4jw4jEFKQIDAQAB"
    ),
    "staticHost": "static.portal101.cn",
    "protocol": "https",
    "noNeedRetry": True,
}


def _browser_path() -> str | None:
    try:
        from modules.signin.core import find_browser

        path = find_browser()
        if path:
            return path
    except Exception:
        pass
    for name in ("chrome", "chromium", "msedge", "google-chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def get_device_token() -> str:
    """Run the official browser SDK in an isolated, short-lived browser page."""
    req = urllib.request.Request(SDK_URL, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            if response.status != 200:
                raise RuntimeError("SDK download failed")
            source = response.read(2_000_000).decode("utf-8")
    except Exception as exc:
        raise RuntimeError("无法连接小黑盒设备令牌服务") from exc
    if hashlib.sha256(source.encode("utf-8")).hexdigest() != SDK_SHA256:
        raise RuntimeError("小黑盒设备令牌组件已更新，请升级程序后重试")

    browser_path = _browser_path()
    try:
        with sync_playwright() as playwright:
            kwargs = {"headless": True, "args": ["--no-sandbox"]}
            if browser_path:
                kwargs["executable_path"] = browser_path
            browser = playwright.chromium.launch(**kwargs)
            try:
                context = browser.new_context(locale="zh-CN")
                page = context.new_page()

                page.goto("https://www.xiaoheihe.cn/", wait_until="domcontentloaded", timeout=15000)
                page.evaluate("""config => {
                  window._smConf = {...config, onError: code => window.__heyboxSdkError = String(code)};
                  window._smReadyFuncs = [];
                  window.SMSdk = {};
                }""", DEVICE_TOKEN_CONFIG)
                page.add_script_tag(content=source)
                # The SDK first creates an encrypted browser profile. Its final
                # B-prefixed device token arrives asynchronously from deviceprofile/v4.
                page.evaluate("window.SMSdk?.createDeviceId?.()")
                for _ in range(100):
                    token = page.evaluate("window.SMSdk?.getDeviceId?.() || null")
                    if isinstance(token, str) and TOKEN_RE.fullmatch(token):
                        return token
                    page.wait_for_timeout(100)
                error = page.evaluate("window.__heyboxSdkError || ''")
                suffix = f"（SDK {error}）" if error else ""
                raise RuntimeError("设备令牌生成超时" + suffix)
            finally:
                browser.close()
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError("无法启动浏览器生成设备令牌，请确认 Chrome 或 Edge 可用") from exc
