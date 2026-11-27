"""Operate supported official games through their rendered controls.

The tower game keeps its physics, timing, session and settlement in its own
page.  This adapter only clicks visible controls.  A result page is evidence
that the game settled, not evidence that a medal task was credited; callers
must re-read GetTaskList before counting a completed task.
"""
from __future__ import annotations

import math
import time
from urllib.parse import parse_qs, urlsplit


TOWER_HOST = "game.y.qq.com"
TOWER_PATH = "/tmegames/web_pages/qm_tonglou/index.html"


class BrowserGameError(Exception):
    """A real game could not be completed through its official page."""


def supports_browser_game(url: str) -> bool:
    """Recognize only the inspected production page, never preview/mock mode."""
    try:
        parsed = urlsplit(str(url or ""))
        query = parse_qs(parsed.query, keep_blank_values=True)
        return (parsed.scheme == "https" and parsed.hostname == TOWER_HOST
                and parsed.port in (None, 443) and not parsed.username
                and not parsed.password and not parsed.fragment
                and parsed.path == TOWER_PATH
                and not any(key.lower() in ("debug", "page")
                            or "preview" in key.lower() or "mock" in key.lower()
                            for key in query))
    except (TypeError, ValueError):
        return False


def _visible(page, selector: str):
    locator = page.locator(selector).first
    return locator if locator.is_visible() else None


def _aligned(page) -> bool:
    hanging = _visible(page, ".stage-hanging-block")
    if hanging is None or _visible(page, ".stage-falling-block") is not None:
        return False
    tower = page.locator(".stage-tower-block")
    target = tower.last if tower.count() else page.locator(".stage-base").first
    moving_box, target_box = hanging.bounding_box(), target.bounding_box()
    if not moving_box or not target_box:
        return False
    moving_center = moving_box["x"] + moving_box["width"] / 2
    target_center = target_box["x"] + target_box["width"] / 2
    tolerance = max(3, min(moving_box["width"], target_box["width"]) * 0.16)
    return abs(moving_center - target_center) <= tolerance


def play_browser_game(page, *, timeout_seconds: float = 120) -> dict:
    """Play one tower session; skip ads and return only after page settlement.

    ``page`` must already be loaded in the caller's authenticated context.
    No game state, timer, score, bridge, request, ad token or result is changed.
    The adapter never clicks restart, share, purchases or ad reward buttons.
    """
    if not supports_browser_game(page.url):
        raise BrowserGameError("此游戏页面尚未接入自动操作")
    try:
        duration = float(timeout_seconds)
    except (TypeError, ValueError):
        raise BrowserGameError("游戏执行时限必须是 0 到 300 秒之间的数字") from None
    if not math.isfinite(duration) or not 0 < duration <= 300:
        raise BrowserGameError("游戏执行时限必须是 0 到 300 秒之间的数字")

    deadline = time.monotonic() + duration
    started = False
    released = 0
    last_release = 0.0
    try:
        while time.monotonic() < deadline:
            if not supports_browser_game(page.url):
                raise BrowserGameError("游戏跳转到未支持页面，已停止自动操作")
            if _visible(page, ".boot-error"):
                raise BrowserGameError("盖楼页面登录或请求 SDK 未就绪，请在官方客户端查看")
            if _visible(page, ".settlement-page"):
                if not started or not released:
                    raise BrowserGameError("未确认本次实际游玩，不能将结果页记为完成")
                return {"played": True, "settled": True, "verified": False,
                        "released": released, "msg": "盖楼已实际游玩并结算，等待服务端核对任务进度"}

            if not started:
                if _visible(page, ".home-ad-stamina"):
                    raise BrowserGameError("盖楼免费体力已用完，不自动播放广告或购买体力")
                start = _visible(page, ".home-start")
                if start is not None:
                    if (start.get_attribute("aria-disabled") == "true"
                            or not start.is_enabled()
                            or "is-disabled" in (start.get_attribute("class") or "").split()):
                        raise BrowserGameError("盖楼暂不可开始，请核对免费体力和任务前置条件")
                    start.click(timeout=1500)
                    started = True
            else:
                # These are the official options that continue without an ad.
                skip = _visible(page, ".boost-skip")
                give_up = _visible(page, ".failure-give-up")
                if skip is not None:
                    skip.click(timeout=1500)
                elif give_up is not None:
                    give_up.click(timeout=1500)
                elif (_visible(page, ".game-stage") is not None
                      and time.monotonic() - last_release >= 0.65
                      and _aligned(page)):
                    page.locator(".game-stage").click(position={"x": 12, "y": 12}, timeout=1500)
                    released += 1
                    last_release = time.monotonic()
            page.wait_for_timeout(80)
    except BrowserGameError:
        raise
    except Exception:
        # Playwright exceptions can contain full authenticated URLs.  Keep
        # diagnostics safe and let the caller re-read actual task progress.
        raise BrowserGameError("盖楼页面操作失败，未确认结算，请刷新任务进度") from None
    raise BrowserGameError("盖楼执行超时，未确认结算，请在官方页面继续或稍后刷新进度")
