"""Interaction tasks (like / collect / comment) with a Redis-backed delayed queue.

Queue: sorted set, member = task id, score = earliest run time (unix seconds).
Rate limits per XHS account: a daily cap and a minimum gap between two actions.
"""

import time
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import InteractionTask, XhsAccount
from app.redis_client import get_redis
from app.xhs.base import get_connector

QUEUE_KEY = "xhs:interaction:queue"
ACTIONS = ("like", "collect", "comment")


def _daily_key(account_id: int) -> str:
    return f"xhs:interaction:daily:{account_id}:{datetime.now():%Y%m%d}"


def _gap_key(account_id: int) -> str:
    return f"xhs:interaction:gap:{account_id}"


async def enqueue(task_id: int, delay: float = 0) -> None:
    await get_redis().zadd(QUEUE_KEY, {str(task_id): time.time() + delay})


async def create_task(
    db: AsyncSession, account: XhsAccount, target_note: str, action: str, comment_text: str = ""
) -> InteractionTask:
    # Comments are public speech under the user's name: they wait for the user's
    # approval unless the account has opted into full automation.
    needs_approval = action == "comment" and not account.auto_interaction
    task = InteractionTask(
        user_id=account.user_id,
        account_id=account.id,
        target_note=target_note,
        action=action,
        comment_text=comment_text,
        status="pending_approval" if needs_approval else "queued",
    )
    db.add(task)
    await db.commit()
    if task.status == "queued":
        await enqueue(task.id)
    return task


async def approve_task(db: AsyncSession, task: InteractionTask, comment_text: str | None = None) -> None:
    if comment_text is not None:
        task.comment_text = comment_text
    task.status = "queued"
    await db.commit()
    await enqueue(task.id)


async def claim_due(limit: int = 10) -> list[int]:
    """Atomically pop task ids whose run time has come."""
    r = get_redis()
    ids = await r.zrangebyscore(QUEUE_KEY, 0, time.time(), start=0, num=limit)
    return [int(i) for i in ids if await r.zrem(QUEUE_KEY, i)]


async def process_task(db: AsyncSession, task_id: int) -> str:
    """Run one task. Returns the resulting status ('requeued' if rate limited)."""
    s = get_settings()
    r = get_redis()
    task = await db.get(InteractionTask, task_id)
    if task is None or task.status != "queued":
        return "skipped"
    account = await db.get(XhsAccount, task.account_id)

    if int(await r.get(_daily_key(task.account_id)) or 0) >= s.interaction_daily_limit:
        task.status, task.error = "failed", "已达到该账号的每日互动上限"
        await db.commit()
        return task.status

    gap = s.interaction_min_interval_seconds
    if gap > 0 and not await r.set(_gap_key(task.account_id), "1", nx=True, ex=gap):
        ttl = max(await r.ttl(_gap_key(task.account_id)), 1)
        await enqueue(task.id, delay=ttl)
        return "requeued"

    task.status = "running"
    await db.commit()
    try:
        connector = get_connector(account.connector, account.credential)
        if task.action == "like":
            await connector.like(task.target_note)
        elif task.action == "collect":
            await connector.collect(task.target_note)
        else:
            await connector.comment(task.target_note, task.comment_text)
        task.status = "done"
        await r.incr(_daily_key(task.account_id))
        await r.expire(_daily_key(task.account_id), 60 * 60 * 48)
    except Exception as exc:  # connector failures are recorded, never crash the worker
        task.status, task.error = "failed", str(exc)[:500]
    task.executed_at = datetime.now()
    await db.commit()
    return task.status


async def usage_today(account_id: int) -> int:
    return int(await get_redis().get(_daily_key(account_id)) or 0)
