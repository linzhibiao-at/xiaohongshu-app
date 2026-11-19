"""互动冷却与节流。

在发出请求前拦住两类高风险模式：
  1. 同一篇笔记的重复操作（例如 2 分钟内连发两条评论）
  2. 单位时间内的写操作总量（点赞 + 收藏 + 评论 + 回复）

状态落盘到 ``~/.xiaohongshu-skills/interaction_state.json``，跨进程共享。
所有阈值可用环境变量覆盖，设 ``XHS_COOLDOWN_DISABLED=1`` 可整体关闭。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .errors import CooldownError

# 同一笔记重复操作的冷却时长（秒）
COOLDOWN_BY_ACTION = {
    "comment": int(os.getenv("XHS_COOLDOWN_COMMENT_SEC", "1200")),   # 20 分钟
    "reply": int(os.getenv("XHS_COOLDOWN_REPLY_SEC", "600")),        # 10 分钟
    "like": int(os.getenv("XHS_COOLDOWN_LIKE_SEC", "180")),          # 3 分钟
    "favorite": int(os.getenv("XHS_COOLDOWN_FAVORITE_SEC", "300")),  # 5 分钟
}

ACTION_LABELS = {
    "comment": "评论",
    "reply": "回复",
    "like": "点赞",
    "favorite": "收藏",
}

# 滑动窗口内的写操作上限
HOURLY_LIMIT = int(os.getenv("XHS_WRITE_HOURLY_LIMIT", "20"))
WINDOW_SEC = 3600

# 状态文件保留时长（超过一天的单笔记记录会被清理）
RECORD_TTL_SEC = 86400

DISABLED = os.getenv("XHS_COOLDOWN_DISABLED", "") == "1"

STATE_DIR = Path(os.getenv("XHS_STATE_DIR") or Path.home() / ".xiaohongshu-skills")
STATE_FILE = STATE_DIR / "interaction_state.json"


def _now() -> float:
    return time.time()


def _load() -> dict:
    """读取状态文件，损坏或缺失时返回空状态。"""
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "actions": {}, "writes": []}


def _save(state: dict) -> None:
    """原子写入状态文件（先写临时文件再替换）。"""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError:
        # 状态落盘失败不应阻断正常互动
        pass


def _prune(state: dict) -> None:
    """清理窗口外与过期的记录。"""
    now = _now()
    state["writes"] = [
        float(t) for t in state.get("writes", []) if now - float(t) < WINDOW_SEC
    ]

    actions = state.setdefault("actions", {})
    for key in list(actions):
        entry = actions[key]
        if not isinstance(entry, dict):
            del actions[key]
            continue
        if now - float(entry.get("last_ts", 0) or 0) > RECORD_TTL_SEC:
            del actions[key]


def _fmt_duration(sec: float) -> str:
    total = int(sec + 0.999)
    if total < 60:
        return f"{total} 秒"
    if total >= 3600:
        hours, rest = divmod(total, 3600)
        minutes = rest // 60
        return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"
    return f"{total // 60} 分 {total % 60} 秒"


def ensure_allowed(feed_id: str, action: str) -> None:
    """检查本次互动是否允许，不允许则抛 CooldownError。

    Args:
        feed_id: 目标笔记 ID。
        action: comment / reply / like / favorite。

    Raises:
        CooldownError: 命合同笔记冷却或小时总量上限。
    """
    if DISABLED:
        return

    state = _load()
    _prune(state)
    now = _now()
    label = ACTION_LABELS.get(action, action)

    # 1) 同一笔记重复操作
    entry = state.get("actions", {}).get(f"{feed_id}:{action}") or {}
    last_ts = float(entry.get("last_ts", 0) or 0)
    cooldown = COOLDOWN_BY_ACTION.get(action, 0)
    if cooldown and last_ts and now - last_ts < cooldown:
        remaining = cooldown - (now - last_ts)
        raise CooldownError(
            f"该笔记 {_fmt_duration(now - last_ts)}前刚{label}过，"
            f"需再等 {_fmt_duration(remaining)}；"
            "如需强制继续请加 --force（风控风险自负）",
            remaining_sec=remaining,
        )

    # 2) 滑动窗口内的写操作总量
    writes = state.get("writes", [])
    if HOURLY_LIMIT and len(writes) >= HOURLY_LIMIT:
        remaining = WINDOW_SEC - (now - min(writes))
        raise CooldownError(
            f"最近 1 小时已执行 {len(writes)} 次互动（上限 {HOURLY_LIMIT}），"
            f"需再等 {_fmt_duration(remaining)}；"
            "如需强制继续请加 --force（风控风险自负）",
            remaining_sec=remaining,
        )


def record(feed_id: str, action: str) -> None:
    """记录一次已完成的互动。"""
    if DISABLED:
        return

    state = _load()
    _prune(state)
    now = _now()

    entry = state.setdefault("actions", {}).setdefault(
        f"{feed_id}:{action}", {"count": 0, "last_ts": 0}
    )
    entry["count"] = int(entry.get("count", 0)) + 1
    entry["last_ts"] = now
    state.setdefault("writes", []).append(now)

    _save(state)


def snapshot() -> dict:
    """返回当前状态摘要（用于排查，不落盘）。"""
    state = _load()
    _prune(state)
    return {
        "disabled": DISABLED,
        "hourly_limit": HOURLY_LIMIT,
        "writes_last_hour": len(state.get("writes", [])),
        "cooldown_seconds": dict(COOLDOWN_BY_ACTION),
        "state_file": str(STATE_FILE),
        "tracked_notes": len(state.get("actions", {})),
    }
