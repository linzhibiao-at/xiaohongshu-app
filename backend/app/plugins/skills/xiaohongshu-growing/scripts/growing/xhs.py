"""xiaohongshu-skills CLI 的调用封装。

职责边界：
- 本模块只负责"取数"，不做任何业务判断。
- 所有小红书操作都通过 `python <xiaohongshu-skills>/scripts/cli.py` 完成。
- 内置节流，避免连续访问触发风控。
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import THROTTLE_EVERY, THROTTLE_SLEEP_RANGE, XHS_CLI, xhs_python
from .metrics import parse_count


class XhsError(RuntimeError):
    """小红书 CLI 调用失败。"""


class NotLoggedInError(XhsError):
    """未登录，需要先跑 xhs-auth 流程。"""


_detail_calls = 0


def _throttle() -> None:
    """每 N 次详情请求后随机休息，模拟人类阅读节奏。"""
    global _detail_calls
    _detail_calls += 1
    if _detail_calls % THROTTLE_EVERY == 0:
        sleep = random.uniform(*THROTTLE_SLEEP_RANGE)
        print(f"[growing] 防风控等待 {sleep:.0f}s", file=sys.stderr)
        time.sleep(sleep)


def _extract_json(stdout: str) -> dict:
    """CLI 正常只输出 JSON，这里做一次容错提取。"""
    text = stdout.strip()
    if not text:
        raise XhsError("CLI 无输出")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise XhsError(f"CLI 输出不是合法 JSON：{text[:200]}") from None


def run_cli(args: list[str], timeout: int = 180) -> dict:
    """运行 xiaohongshu-skills CLI，返回解析后的 JSON。"""
    cmd = [xhs_python(), str(XHS_CLI), *args]
    if not Path(XHS_CLI).exists():
        raise XhsError(f"找不到底层 CLI：{XHS_CLI}")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode == 1:
        raise NotLoggedInError(
            "小红书未登录，请先执行登录：python scripts/cli.py login"
        )
    if proc.returncode not in (0,):
        detail = (proc.stdout or proc.stderr or "").strip()[:500]
        raise XhsError(f"CLI 返回码 {proc.returncode}：{detail}")
    return _extract_json(proc.stdout)


def check_login() -> tuple[bool, str]:
    """检查登录状态，返回 (是否已登录, 错误说明)。

    未登录时底层 CLI 会顺带生成并打开登录二维码，正好用于引导用户。
    """
    try:
        run_cli(["check-login"], timeout=120)
        return True, ""
    except NotLoggedInError as exc:
        return False, str(exc)
    except XhsError as exc:
        return False, str(exc)


def search(
    keyword: str,
    sort_by: str = "",
    publish_time: str = "",
    note_type: str = "",
    location: str = "",
) -> list[dict]:
    """搜索笔记，返回 feed 列表（含 id/xsecToken/noteCard）。"""
    args = ["search-feeds", "--keyword", keyword]
    if sort_by:
        args += ["--sort-by", sort_by]
    if publish_time:
        args += ["--publish-time", publish_time]
    if note_type:
        args += ["--note-type", note_type]
    if location:
        args += ["--location", location]
    data = run_cli(args, timeout=180)
    return list(data.get("feeds") or [])


def hot_suggest(keyword: str) -> dict:
    """采集站内搜索联想词与相关话题。

    返回 {"keyword", "suggestions": [...], "tags": [...]}。
    联想词按用户真实搜索行为排序，是站内需求信号，来源等级 L1。
    """
    _throttle()
    data = run_cli(["hot-suggest", "--keyword", keyword], timeout=180)
    if data.get("success") is False:
        raise XhsError(data.get("error") or "搜索联想词采集失败")
    # 未登录时会静默返回空列表，不拦一下就会被当成“这个赛道没人搜”
    if data.get("login_required"):
        raise NotLoggedInError(
            "小红书未登录，搜索页被重定向到登录页：请先执行 python scripts/cli.py login"
        )
    return data


def feed_detail(
    feed_id: str,
    xsec_token: str,
    load_comments: bool = False,
    max_comments: int = 20,
) -> dict:
    """获取笔记详情。load_comments=True 时会额外加载评论（更慢，风控更敏感）。"""
    args = ["get-feed-detail", "--feed-id", feed_id, "--xsec-token", xsec_token]
    if load_comments:
        args += [
            "--load-all-comments",
            "--max-comment-items",
            str(max_comments),
            "--scroll-speed",
            "slow",
        ]
    _throttle()
    data = run_cli(args, timeout=240)
    if data.get("success") is False:
        raise XhsError(data.get("error") or "笔记详情获取失败")
    return data


def user_profile(user_id: str, xsec_token: str = "") -> dict:
    """获取用户主页（粉丝数 + 笔记列表）。"""
    data = run_cli(
        ["user-profile", "--user-id", user_id, "--xsec-token", xsec_token], timeout=180
    )
    return data


# ─── 字段归一化 ──────────────────────────────────────────────────────────────


def normalize_feed(feed: dict, lane: str = "", keyword: str = "") -> dict:
    """把搜索结果里的 feed 转成 notes 表可写入的结构。

    CLI 的 `Feed.to_dict()` 输出是扁平的（displayTitle/user/interactInfo 都在顶层），
    这里同时兼容嵌套 noteCard 写法，避免上游改结构时静默丢字段。
    """
    card = feed.get("noteCard") or feed.get("note_card") or feed
    interact = card.get("interactInfo") or card.get("interact_info") or {}
    user = card.get("user") or {}
    return {
        "feed_id": feed.get("id", ""),
        "xsec_token": feed.get("xsecToken", "") or feed.get("xsec_token", ""),
        "title": card.get("displayTitle", "") or card.get("display_title", ""),
        "note_type": card.get("type", ""),
        "author_id": user.get("userId", "") or user.get("user_id", ""),
        "author_name": user.get("nickname", "") or user.get("nick_name", ""),
        "liked": parse_count(interact.get("likedCount")).value,
        "collected": parse_count(interact.get("collectedCount")).value,
        "comment": parse_count(interact.get("commentCount")).value,
        "shared": parse_count(interact.get("sharedCount")).value,
        "liked_raw": str(interact.get("likedCount") or ""),
        "collected_raw": str(interact.get("collectedCount") or ""),
        "comment_raw": str(interact.get("commentCount") or ""),
        "cover": feed.get("cover", ""),
        "lane": lane,
        "keyword": keyword,
        "publish_ts": 0,
    }


def normalize_detail(detail: dict, lane: str = "", keyword: str = "") -> dict:
    """把笔记详情转成 notes 表可写入的结构。"""
    note = detail.get("note") or {}
    interact = note.get("interactInfo") or {}
    user = note.get("user") or {}
    return {
        "feed_id": note.get("noteId", ""),
        "xsec_token": note.get("xsecToken", ""),
        "title": note.get("title", ""),
        "desc": note.get("desc", ""),
        "body": note.get("body", ""),
        "tags": note.get("tags", []) or [],
        "note_type": note.get("type", ""),
        "author_id": user.get("userId", ""),
        "author_name": user.get("nickname", ""),
        "liked": parse_count(interact.get("likedCount")).value,
        "collected": parse_count(interact.get("collectedCount")).value,
        "comment": parse_count(interact.get("commentCount")).value,
        "shared": parse_count(interact.get("sharedCount")).value,
        "liked_raw": str(interact.get("likedCount") or ""),
        "collected_raw": str(interact.get("collectedCount") or ""),
        "comment_raw": str(interact.get("commentCount") or ""),
        "publish_ts": int(note.get("time") or 0),
        "ip_location": note.get("ipLocation", ""),
        "lane": lane,
        "keyword": keyword,
    }


def normalize_author(profile: dict, lane: str = "") -> dict:
    """把用户主页转成 authors 表可写入的结构。"""
    basic = profile.get("basicInfo") or {}
    interactions = {
        i.get("name", ""): i.get("count", "") for i in profile.get("interactions") or []
    }
    fans_raw = interactions.get("粉丝", "") or interactions.get("fans", "")
    return {
        "user_id": basic.get("userId", "") or profile.get("userId", ""),
        "nickname": basic.get("nickname", ""),
        "red_id": basic.get("redId", ""),
        "desc": basic.get("desc", ""),
        "fans": parse_count(fans_raw).value,
        "fans_raw": str(fans_raw),
        "following": parse_count(interactions.get("关注", "")).value,
        "like_collected": parse_count(
            interactions.get("获赞与收藏", "") or interactions.get("获赞", "")
        ).value,
        "ip_location": basic.get("ipLocation", ""),
        "lane": lane,
    }


def comments_of(detail: dict) -> list[dict]:
    """提取评论，供选题钩子与痛点分析使用。"""
    return list(detail.get("comments") or [])


def parse_own_profile_url(url: str) -> tuple[str, str]:
    """从主页链接解析 user_id 与 xsec_token。"""
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    user_id = ""
    if "profile" in parts:
        idx = parts.index("profile")
        if idx + 1 < len(parts):
            user_id = parts[idx + 1]
    elif parts:
        user_id = parts[-1]
    token = (parse_qs(parsed.query).get("xsec_token") or [""])[0]
    if not user_id:
        raise ValueError(f"无法从链接解析 userId：{url}")
    return user_id, token


def as_metrics(notes: list[dict]) -> list[Any]:
    """把 notes 表行转成 NoteMetrics，供指标计算使用。"""
    from .metrics import NoteMetrics

    out = []
    for n in notes:
        out.append(
            NoteMetrics(
                feed_id=n.get("feed_id", ""),
                title=n.get("title", ""),
                author_id=n.get("author_id", ""),
                author_name=n.get("author_name", ""),
                note_type=n.get("note_type", ""),
                liked=int(n.get("liked", 0)),
                collected=int(n.get("collected", 0)),
                comment=int(n.get("comment", 0)),
                shared=int(n.get("shared", 0)),
                publish_ts=int(n.get("publish_ts", 0)),
                tags=n.get("tags", []) or [],
            )
        )
    return out
