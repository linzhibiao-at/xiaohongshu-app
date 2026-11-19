import json

from sqlalchemy import select

from app.agents.plugins import current_user_id
from app.db import SessionLocal
from app.models import XhsAccount
from app.services.analytics import account_report


async def list_my_accounts() -> str:
    """列出当前用户绑定的小红书账号（id、昵称、赛道、粉丝数）。"""
    async with SessionLocal() as db:
        rows = await db.scalars(select(XhsAccount).where(XhsAccount.user_id == current_user_id.get()))
        return json.dumps(
            [{"id": a.id, "nickname": a.nickname, "niche": a.niche, "followers": a.followers} for a in rows],
            ensure_ascii=False,
        )


async def get_traffic_report(account_id: int) -> str:
    """获取某个小红书账号的笔记流量报告。

    Args:
        account_id: 账号 id，可通过 list_my_accounts 获得。
    """
    async with SessionLocal() as db:
        account = await db.get(XhsAccount, account_id)
        if account is None or account.user_id != current_user_id.get():
            return "账号不存在或无权访问"
        report = await account_report(db, account_id)
    if not report["note_count"]:
        return "该账号还没有同步过笔记数据，请先在「流量分析」页同步。"
    report["notes"] = report["notes"][:20]
    return json.dumps(report, ensure_ascii=False)


TOOLS = [list_my_accounts, get_traffic_report]
