"""点赞/收藏操作，对应 Go xiaohongshu/like_favorite.go。"""

from __future__ import annotations

import json
import logging
import time

from .cdp import Page
from .cooldown import ensure_allowed, record
from .errors import NoFeedDetailError
from .human import READ_BEFORE_FAVORITE, READ_BEFORE_LIKE, simulate_feed_reading
from .selectors import COLLECT_BUTTON, LIKE_BUTTON
from .types import ActionResult
from .urls import make_feed_detail_url

logger = logging.getLogger(__name__)

# 从 __INITIAL_STATE__ 读取互动状态的 JS
_GET_INTERACT_STATE_JS = """
(() => {
    if (window.__INITIAL_STATE__ &&
        window.__INITIAL_STATE__.note &&
        window.__INITIAL_STATE__.note.noteDetailMap) {
        return JSON.stringify(window.__INITIAL_STATE__.note.noteDetailMap);
    }
    return "";
})()
"""


def _get_interact_state(page: Page, feed_id: str) -> tuple[bool, bool]:
    """读取笔记的点赞/收藏状态。

    Returns:
        (liked, collected)

    Raises:
        NoFeedDetailError: 无法获取状态。
    """
    result = page.evaluate(_GET_INTERACT_STATE_JS)
    if not result:
        raise NoFeedDetailError()

    note_detail_map = json.loads(result)
    detail = note_detail_map.get(feed_id)
    if not detail and len(note_detail_map) == 1:
        detail = next(iter(note_detail_map.values()))

    if not detail:
        raise NoFeedDetailError()

    interact = detail.get("note", {}).get("interactInfo", {})
    return interact.get("liked", False), interact.get("collected", False)


def _prepare_page(
    page: Page,
    feed_id: str,
    xsec_token: str,
    read_range: tuple[float, float] = READ_BEFORE_LIKE,
) -> None:
    """导航到 feed 详情页，并模拟一段阅读停留。"""
    url = make_feed_detail_url(feed_id, xsec_token)
    page.navigate(url)
    page.wait_for_load()
    page.wait_dom_stable()
    simulate_feed_reading(page, read_range)


# ========== 点赞 ==========


def like_feed(
    page: Page, feed_id: str, xsec_token: str, force: bool = False
) -> ActionResult:
    """点赞笔记（幂等：已点赞则跳过）。"""
    _prepare_page(page, feed_id, xsec_token, READ_BEFORE_LIKE)
    return _toggle_like(page, feed_id, target_liked=True, force=force)


def unlike_feed(page: Page, feed_id: str, xsec_token: str) -> ActionResult:
    """取消点赞（幂等：未点赞则跳过）。"""
    _prepare_page(page, feed_id, xsec_token, READ_BEFORE_LIKE)
    return _toggle_like(page, feed_id, target_liked=False)


def _toggle_like(
    page: Page, feed_id: str, target_liked: bool, force: bool = False
) -> ActionResult:
    """执行点赞/取消点赞操作。"""
    action_name = "点赞" if target_liked else "取消点赞"

    try:
        liked, _ = _get_interact_state(page, feed_id)
    except NoFeedDetailError:
        logger.warning("无法读取互动状态，直接点击")
        liked = not target_liked  # 强制执行点击

    # 幂等检查
    if liked == target_liked:
        logger.info("feed %s 已%s，跳过", feed_id, action_name)
        return ActionResult(feed_id=feed_id, success=True, message=f"已{action_name}")

    # 取消点赞不受限，只限制会给账号加权的正向操作
    if target_liked and not force:
        ensure_allowed(feed_id, "like")

    # 点击
    page.click_element(LIKE_BUTTON)
    time.sleep(3)

    # 验证
    try:
        liked, _ = _get_interact_state(page, feed_id)
        if liked == target_liked:
            if target_liked:
                record(feed_id, "like")
            logger.info("feed %s %s成功", feed_id, action_name)
            return ActionResult(feed_id=feed_id, success=True, message=f"{action_name}成功")
    except NoFeedDetailError:
        pass

    # 重试一次
    logger.warning("feed %s %s可能未成功，重试", feed_id, action_name)
    page.click_element(LIKE_BUTTON)
    time.sleep(2)

    if target_liked:
        record(feed_id, "like")
    return ActionResult(feed_id=feed_id, success=True, message=f"{action_name}已执行")


# ========== 收藏 ==========


def favorite_feed(
    page: Page, feed_id: str, xsec_token: str, force: bool = False
) -> ActionResult:
    """收藏笔记（幂等：已收藏则跳过）。"""
    _prepare_page(page, feed_id, xsec_token, READ_BEFORE_FAVORITE)
    return _toggle_favorite(page, feed_id, target_collected=True, force=force)


def unfavorite_feed(page: Page, feed_id: str, xsec_token: str) -> ActionResult:
    """取消收藏（幂等：未收藏则跳过）。"""
    _prepare_page(page, feed_id, xsec_token, READ_BEFORE_FAVORITE)
    return _toggle_favorite(page, feed_id, target_collected=False)


def _wait_collect_button(page: Page, timeout: float = 5.0, interval: float = 0.2) -> bool:
    """等待收藏按钮出现。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.has_element(COLLECT_BUTTON):
            return True
        time.sleep(interval)
    return False


def _wait_collected_state(
    page: Page,
    feed_id: str,
    target_collected: bool,
    timeout: float = 3.0,
    interval: float = 0.3,
) -> bool:
    """短轮询验证收藏状态是否达到目标。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _, collected = _get_interact_state(page, feed_id)
            if collected == target_collected:
                return True
        except NoFeedDetailError:
            pass
        time.sleep(interval)
    return False


def _toggle_favorite(
    page: Page, feed_id: str, target_collected: bool, force: bool = False
) -> ActionResult:
    """执行收藏/取消收藏操作。"""
    action_name = "收藏" if target_collected else "取消收藏"

    try:
        _, collected = _get_interact_state(page, feed_id)
    except NoFeedDetailError:
        logger.warning("无法读取互动状态，直接点击")
        collected = not target_collected

    # 幂等检查
    if collected == target_collected:
        logger.info("feed %s 已%s，跳过", feed_id, action_name)
        return ActionResult(feed_id=feed_id, success=True, message=f"已{action_name}")

    if target_collected and not force:
        ensure_allowed(feed_id, "favorite")

    if not _wait_collect_button(page, timeout=5.0):
        logger.error("feed %s 未找到收藏按钮: %s", feed_id, COLLECT_BUTTON)
        return ActionResult(
            feed_id=feed_id, success=False, message=f"{action_name}失败：未找到收藏按钮"
        )

    for attempt in range(2):
        if attempt > 0:
            logger.warning("feed %s %s首次未确认成功，重试", feed_id, action_name)

        try:
            page.click_element(COLLECT_BUTTON)
        except Exception as e:
            logger.warning("feed %s 点击收藏按钮失败（第%d次）: %s", feed_id, attempt + 1, e)
            continue

        if _wait_collected_state(page, feed_id, target_collected, timeout=3.0, interval=0.3):
            if target_collected:
                record(feed_id, "favorite")
            logger.info("feed %s %s成功", feed_id, action_name)
            return ActionResult(feed_id=feed_id, success=True, message=f"{action_name}成功")

    logger.error("feed %s %s未确认成功", feed_id, action_name)
    return ActionResult(feed_id=feed_id, success=False, message=f"{action_name}失败：状态未变化")
