"""Interaction worker: `python -m app.worker`."""

import asyncio
import logging
from datetime import datetime

from app.db import SessionLocal, init_db
from app.redis_client import get_redis
from app.services.interaction import claim_due, process_task

log = logging.getLogger("xhs.worker")


async def run_once() -> int:
    task_ids = await claim_due()
    for task_id in task_ids:
        async with SessionLocal() as db:
            status = await process_task(db, task_id)
        log.info("task %s -> %s", task_id, status)
    return len(task_ids)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    await init_db()
    log.info("worker started")
    while True:
        try:
            await get_redis().set("xhs:worker:heartbeat", datetime.now().isoformat(timespec="seconds"), ex=120)
            if not await run_once():
                await asyncio.sleep(1)
        except Exception:
            log.exception("worker loop error")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
