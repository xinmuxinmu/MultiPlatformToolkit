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
FLIP_HOST = "y.qq.com"
FLIP_PATH = "/m/client/starlight_game/index.html"


class BrowserGameError(Exception):
    """A real game could not be completed through its official page."""


def supports_browser_game(url: str) -> bool:
    """Recognize only the inspected production page, never preview/mock mode."""
    if supports_flip_game(url):
        return True
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


def supports_flip_game(url: str) -> bool:
    """Accept only the singer-medal memory game and its server-provided issue."""
    try:
        parsed = urlsplit(str(url or ""))
        query = parse_qs(parsed.query, keep_blank_values=True)
        allowed = {"issue", "biz", "_hidehd", "_miniplayer",
                   "_fontscale", "_button", "actid"}
        if (parsed.scheme != "https" or parsed.hostname != FLIP_HOST
                or parsed.port not in (None, 443) or parsed.username or parsed.password
                or parsed.fragment or parsed.path != FLIP_PATH
                or set(query) - allowed):
            return False
        issue = query.get("issue", [])
        biz = query.get("biz", [])
        return (len(issue) == 1 and issue[0].isdigit() and len(issue[0]) <= 20
                and biz == ["singermedal"])
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
    """Play a supported official browser game through its rendered controls.

    ``page`` must already be loaded in the caller's authenticated context.
    No game state, timer, score, bridge, request, ad token or result is changed.
    The adapter never clicks restart, share, purchases or ad reward buttons.
    """
    if not supports_browser_game(page.url):
        raise BrowserGameError("此游戏页面尚未接入自动操作")
    if supports_flip_game(page.url):
        return play_flip_card_game(page, timeout_seconds=timeout_seconds)
    return _play_tower_game(page, timeout_seconds=timeout_seconds)


def _play_tower_game(page, *, timeout_seconds: float) -> dict:
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


def _card_has_class(card, class_name: str) -> bool:
    return class_name in (card.get_attribute("class") or "").split()


def _wait_card_class(page, card, class_name: str, present: bool, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if _card_has_class(card, class_name) == present:
            return True
        page.wait_for_timeout(40)
    return False


def _flip_card(page, card, deadline: float) -> str:
    if _card_has_class(card, "matched") or _card_has_class(card, "flipped"):
        raise BrowserGameError("翻卡页面的牌面状态不符合预期，已停止本局")
    card.click(timeout=1500)
    if not _wait_card_class(page, card, "flipped", True, min(deadline, time.monotonic() + 2)):
        raise BrowserGameError("点击卡片后页面没有确认翻开，已停止本局")
    image = card.locator(".card-front__image").first
    source = image.get_attribute("src") or image.evaluate("el => el.currentSrc")
    if not source:
        raise BrowserGameError("翻开的卡片没有可见图片，无法安全配对")
    return source


def play_flip_card_game(page, *, timeout_seconds: float = 120) -> dict:
    """Play easy mode by clicking cards and matching only revealed artwork."""
    if not supports_flip_game(page.url):
        raise BrowserGameError("翻卡任务入口缺少有效的官方活动参数")
    try:
        duration = float(timeout_seconds)
    except (TypeError, ValueError):
        raise BrowserGameError("游戏执行时限必须是 0 到 300 秒之间的数字") from None
    if not math.isfinite(duration) or not 0 < duration <= 300:
        raise BrowserGameError("游戏执行时限必须是 0 到 300 秒之间的数字")

    deadline = time.monotonic() + duration
    started = False
    pair_attempts = 0
    stage = "检查免费次数与难度"
    try:
        if _visible(page, ".select-page__error") is not None:
            raise BrowserGameError("翻卡活动页加载失败，请刷新任务状态后重试")
        confirm = page.locator(".select-page__confirm-btn")
        confirm.wait_for(state="visible", timeout=min(15000, int(duration * 1000)))
        ready_deadline = min(deadline, time.monotonic() + 15)
        while not confirm.is_enabled() and time.monotonic() < ready_deadline:
            if _visible(page, ".select-page__error") is not None:
                raise BrowserGameError("翻卡活动页加载失败，请刷新任务状态后重试")
            page.wait_for_timeout(100)
        if not confirm.is_enabled():
            raise BrowserGameError("翻卡活动暂不可开始，未消耗次数")
        free_tries = page.locator(
            '.select-page__remain-icon[src*="change_active."]')
        if free_tries.count() < 1:
            raise BrowserGameError("没有可用的免费翻卡次数，不自动看广告或购买")
        easy = page.locator(".select-page__option").filter(has_text="简单").first
        if not easy.count() or not easy.is_visible():
            raise BrowserGameError("翻卡页面未提供简单难度，未开始游戏")
        stage = "启动游戏"
        easy.click(timeout=1500)
        confirm.click(timeout=1500)
        started = True

        stage = "等待游戏牌面"
        page.locator(".game-page").wait_for(state="visible", timeout=15000)
        page.locator(".game-board--visible").wait_for(state="visible", timeout=15000)
        cards = page.locator(".game-board .starlight-card")
        count = cards.count()
        if count != 12:
            raise BrowserGameError("简单难度的牌面数量异常，本局不继续操作")

        unseen = set(range(count))
        known: dict[str, list[int]] = {}
        matched = 0
        stage = "真实翻牌与配对"
        while unseen:
            if time.monotonic() >= deadline:
                raise BrowserGameError("翻卡执行超时，本局结果待服务端核对")
            first = min(unseen)
            unseen.remove(first)
            first_image = _flip_card(page, cards.nth(first), deadline)
            candidates = known.get(first_image) or []
            if candidates:
                second = candidates.pop(0)
                second_image = _flip_card(page, cards.nth(second), deadline)
                pair_attempts += 1
                if first_image != second_image:
                    raise BrowserGameError("已翻开的卡片图片不一致，停止本局避免误报")
                if not (_wait_card_class(page, cards.nth(first), "matched", True, deadline)
                        and _wait_card_class(page, cards.nth(second), "matched", True, deadline)):
                    raise BrowserGameError("页面未确认这组配对，停止本局")
                matched += 1
                continue

            if not unseen:
                raise BrowserGameError("翻卡牌数不是完整配对，本局结果待核对")
            second = min(unseen)
            unseen.remove(second)
            second_image = _flip_card(page, cards.nth(second), deadline)
            pair_attempts += 1
            if first_image == second_image:
                if not (_wait_card_class(page, cards.nth(first), "matched", True, deadline)
                        and _wait_card_class(page, cards.nth(second), "matched", True, deadline)):
                    raise BrowserGameError("页面未确认这组配对，停止本局")
                matched += 1
            else:
                known.setdefault(first_image, []).append(first)
                known.setdefault(second_image, []).append(second)
                if not (_wait_card_class(page, cards.nth(first), "flipped", False, deadline)
                        and _wait_card_class(page, cards.nth(second), "flipped", False, deadline)):
                    raise BrowserGameError("未配对卡片未按游戏规则复位，本局结果待核对")

        for source, positions in known.items():
            while positions:
                if len(positions) < 2:
                    raise BrowserGameError("翻开的牌面无法组成完整配对，本局结果待核对")
                first, second = positions.pop(0), positions.pop(0)
                first_image = _flip_card(page, cards.nth(first), deadline)
                second_image = _flip_card(page, cards.nth(second), deadline)
                pair_attempts += 1
                if first_image != source or second_image != source:
                    raise BrowserGameError("牌面与已记录的可见图案不一致，停止本局")
                if not (_wait_card_class(page, cards.nth(first), "matched", True, deadline)
                        and _wait_card_class(page, cards.nth(second), "matched", True, deadline)):
                    raise BrowserGameError("页面未确认这组配对，停止本局")
                matched += 1

        stage = "等待官方结算"
        success_info = page.locator(".result-modal__info")
        success_info.wait_for(state="visible", timeout=min(
            10000, max(1, int((deadline - time.monotonic()) * 1000))))
        if not started or matched != count // 2:
            raise BrowserGameError("没有确认全部配对结算，本局不记为完成")
        return {"played": True, "settled": True, "verified": False,
                "matched_pairs": matched, "pair_attempts": pair_attempts,
                "msg": "翻卡已通过官方页面实际配对并提交结算，等待服务端核对任务进度"}
    except BrowserGameError:
        raise
    except Exception:
        raise BrowserGameError(
            f"翻卡页面在{stage}时失败，结果待服务端核对；不会自动重开或看广告") from None
