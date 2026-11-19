"""微信授权链路的 CSRF state 与一次性 ticket。

state 是无状态签名 JWT（10 分钟），不落库；ticket 落 Redis，60 秒、取一次即失效，
用于把"已经验过微信身份"这一事实从 302 回调安全地交给前端换 JWT。
"""

import secrets
from datetime import datetime, timedelta, timezone

import jwt

from app.config import get_settings
from app.redis_client import get_redis
from app.security import ALGORITHM

STATE_PURPOSE = "wechat_state"
STATE_TTL = timedelta(minutes=10)
TICKET_TTL_SECONDS = 60
TICKET_PREFIX = "wechat:ticket:"


class TicketStoreError(Exception):
    """ticket 存储（Redis）不可用。"""


def new_state() -> str:
    payload = {
        "purpose": STATE_PURPOSE,
        "nonce": secrets.token_urlsafe(12),
        "exp": datetime.now(timezone.utc) + STATE_TTL,
    }
    return jwt.encode(payload, get_settings().jwt_secret, algorithm=ALGORITHM)


def read_state(state: str) -> bool:
    """只认本模块签发的 state；账密登录的 access token 没有 purpose，会被拒。"""
    if not state:
        return False
    try:
        payload = jwt.decode(state, get_settings().jwt_secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return False
    return payload.get("purpose") == STATE_PURPOSE


async def issue_ticket(user_id: int) -> str:
    ticket = secrets.token_urlsafe(24)
    try:
        await get_redis().set(f"{TICKET_PREFIX}{ticket}", str(user_id), ex=TICKET_TTL_SECONDS)
    except Exception as exc:  # 连接失败/命令失败都视为存储不可用
        raise TicketStoreError("ticket store unavailable") from exc
    return ticket


async def consume_ticket(ticket: str) -> int | None:
    """取到即删除；不存在/已用过返回 None。Redis 异常转 TicketStoreError。"""
    key = f"{TICKET_PREFIX}{ticket}"
    redis = get_redis()
    try:
        try:
            value = await redis.getdel(key)
        except AttributeError:  # 老版本 redis / fakeredis 无 GETDEL
            value = await redis.get(key)
            await redis.delete(key)
    except Exception as exc:
        raise TicketStoreError("ticket store unavailable") from exc
    return int(value) if value else None
