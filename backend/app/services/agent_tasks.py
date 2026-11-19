"""Agent-backed features: growth plans, comment drafts, traffic diagnosis, chat."""

import json
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.plugins import PersonaSpec, get_persona, skills_for
from app.agents.runtime import llm_configured, run_agent, run_agent_stream
from app.db import SessionLocal
from app.models import GrowthPlan, PlanMessage, PlanSession, XhsAccount
from app.redis_client import get_redis

CHAT_TURNS = 8
OFFLINE_NOTICE = "> ⚠️ 未配置大模型（LLM_API_KEY），以下为内置模板生成的通用方案，配置后可生成针对你账号的定制方案。\n\n"
DIALOG_RULES = (
    "\n\n# 对话约定\n"
    "- 信息不足时一次只问最关键的一个问题（赛道、目标、账号现状、每周可投入时间）。\n"
    "- 需要用户做选择时，把选项写成「A. 选项内容」「B. 选项内容」每行一个（2-4 个），放在回复的最后，方便用户直接点选。\n"
    "- 不要在对话里输出完整长方案；信息足够时提示用户点击下方「生成起号方案」。\n"
    "- 保持简洁，每条回复不超过 200 字（用户明确要求展开时除外）。"
)


class PersonaUnavailable(Exception):
    pass


async def _persona(db: AsyncSession, key: str, user_id: int | None) -> PersonaSpec:
    persona = await get_persona(db, key, user_id)
    if persona is None:
        raise PersonaUnavailable(f"人格不存在或已停用: {key}")
    return persona


async def ask(db: AsyncSession, persona_key: str, prompt: str, user_id: int | None) -> str:
    persona = await _persona(db, persona_key, user_id)
    return await run_agent(persona, await skills_for(db, persona), prompt, user_id)


def _template_plan(niche: str, goal: str) -> str:
    return OFFLINE_NOTICE + f"""# {niche}赛道起号方案

**目标**：{goal}

## 1. 账号定位
- 一句话定位：我是「{niche}」领域的___，帮助___解决___。
- 昵称带赛道关键词；简介三行：身份背书 / 提供的价值 / 更新频率。

## 2. 选题库（搜索型 : 推荐型 : 人设型 ≈ 5 : 3 : 2）
- 搜索型：{niche}新手入门、{niche}避坑清单、{niche}工具/好物合集、{niche}常见问题解答、{niche}步骤教程
- 推荐型：{niche}的真实一天、做{niche}后悔没早知道的事、{niche}前后对比
- 人设型：我为什么开始做{niche}、我的{niche}踩坑经历

## 3. 四周节奏
| 周 | 目标 | 动作 |
|---|---|---|
| 1 | 养号建模 | 完善主页；每天真实浏览互动同赛道内容 20-30 分钟；发 2-3 篇测试笔记 |
| 2 | 选题测试 | 隔日更；同选题换 2 种封面标题；记录每篇点击率 |
| 3 | 放大爆款 | 点击率 >10%、互动率 >5% 的笔记做成系列；回复每条评论 |
| 4 | 稳定转粉 | 置顶代表作；做合集；主页强化关注理由 |

## 4. 复盘指标
- 点击率 <5%：改封面和标题
- 互动率 <3%：加强干货密度和互动引导
- 涨粉率 <0.5%：优化主页和人设承接

## 5. 红线
不刷量、不买粉、不进互赞群、不搬运、不留联系方式。
"""


async def generate_plan(plan_id: int) -> None:
    """Background job: fill in a GrowthPlan row."""
    async with SessionLocal() as db:
        plan = await db.get(GrowthPlan, plan_id)
        if plan is None:
            return
        plan.status = "running"
        await db.commit()
        try:
            if not llm_configured():
                plan.content = _template_plan(plan.niche, plan.goal)
            else:
                account = await db.get(XhsAccount, plan.account_id) if plan.account_id else None
                prompt = f"请为我制定一份小红书快速起号涨粉的落地方案。\n- 赛道：{plan.niche}\n- 目标：{plan.goal}\n"
                if account:
                    prompt += (
                        f"- 账号：{account.nickname}（account_id={account.id}，当前粉丝 {account.followers}）\n"
                        "请先用工具查看该账号已有的流量数据，再结合数据制定方案。\n"
                    )
                else:
                    prompt += "- 账号：全新账号，暂无数据。\n"
                prompt += "按起号方法论的 6 个部分输出完整方案，具体到每周的动作。"
                plan.content = await ask(db, plan.persona_key, prompt, plan.user_id)
            plan.status = "done"
        except Exception as exc:
            plan.status, plan.error = "failed", str(exc)[:1000]
        await db.commit()


async def draft_comment(db: AsyncSession, user_id: int, note_context: str) -> str:
    if not llm_configured():
        return ""
    prompt = f"请为下面这篇小红书笔记写一条评论。\n\n笔记内容：\n{note_context[:2000]}"
    return (await ask(db, "community_manager", prompt, user_id)).strip()


async def diagnose(db: AsyncSession, user_id: int, account: XhsAccount, report: dict) -> str:
    slim = {**report, "notes": report["notes"][:20]}
    prompt = (
        f"下面是账号「{account.nickname}」（赛道：{account.niche}）的流量报告，请做一次诊断并给出改进动作。\n\n"
        f"```json\n{json.dumps(slim, ensure_ascii=False)}\n```"
    )
    return await ask(db, "data_analyst", prompt, user_id)


async def chat(db: AsyncSession, user_id: int, persona_key: str, message: str) -> str:
    """Multi-turn chat; the last few turns are kept in Redis."""
    r = get_redis()
    key = f"xhs:chat:{user_id}:{persona_key}"
    history = [json.loads(x) for x in await r.lrange(key, -CHAT_TURNS * 2, -1)]
    prompt = message
    if history:
        lines = "\n".join(f"{'用户' if h['role'] == 'user' else '你'}：{h['text']}" for h in history)
        prompt = f"（此前的对话）\n{lines}\n\n（用户的新消息）\n{message}"
    reply = await ask(db, persona_key, prompt, user_id)
    await r.rpush(
        key,
        json.dumps({"role": "user", "text": message}, ensure_ascii=False),
        json.dumps({"role": "assistant", "text": reply}, ensure_ascii=False),
    )
    await r.ltrim(key, -CHAT_TURNS * 2, -1)
    await r.expire(key, 60 * 60 * 24)
    return reply


async def chat_in_session_stream(
    db: AsyncSession, user_id: int, persona_key: str, account: XhsAccount | None,
    history: list[tuple[str, str]], message: str,
):
    """Stream a consultation reply; history is the session's earlier turns."""
    persona = await _persona(db, persona_key, user_id)
    prompt = (
        f"账号：{account.nickname}（account_id={account.id}，粉丝 {account.followers}，赛道 {account.niche}）\n"
        if account else "账号：全新账号，暂无数据。\n"
    )
    if history:
        prompt += "（此前的对话）\n" + "\n".join(
            f"{'用户' if role == 'user' else '你'}：{content}" for role, content in history
        ) + "\n\n"
    prompt += f"（用户的新消息）\n{message}" + DIALOG_RULES
    async for delta in run_agent_stream(persona, await skills_for(db, persona), prompt, user_id):
        yield delta


async def extract_plan_meta(
    db: AsyncSession, user_id: int, persona_key: str,
    dialog: list[tuple[str, str]], fallback_niche: str,
) -> tuple[str, str]:
    """Ask the model for {niche, goal} from the transcript; fall back to the session title."""
    persona = await _persona(db, persona_key, user_id)
    text = "\n".join(f"{'用户' if role == 'user' else '教练'}：{c}" for role, c in dialog)
    prompt = (
        "从下面的咨询对话中提取赛道和目标，只输出一个 JSON 对象，不要任何其他文字：\n"
        '{"niche": "赛道，不超过20字", "goal": "目标，不超过50字"}\n\n' + text
    )
    try:
        reply = await run_agent(persona, [], prompt, user_id)
        match = re.search(r"\{.*\}", reply, re.S)
        data = json.loads(match.group(0)) if match else {}
        niche = str(data.get("niche") or "").strip()[:64]
        goal = str(data.get("goal") or "").strip()[:255]
        return niche or fallback_niche[:64] or "起号", goal or "AI 对话生成"
    except Exception:
        return fallback_niche[:64] or "起号", "AI 对话生成"


async def generate_plan_from_session(plan_id: int, session_id: int) -> None:
    """Background job: turn a consultation transcript into a GrowthPlan row."""
    async with SessionLocal() as db:
        plan = await db.get(GrowthPlan, plan_id)
        session = await db.get(PlanSession, session_id)
        if plan is None or session is None:
            return
        plan.status = "running"
        await db.commit()
        try:
            rows = (await db.scalars(
                select(PlanMessage)
                .where(PlanMessage.session_id == session.id, PlanMessage.plan_id.is_(None))
                .order_by(PlanMessage.id)
                .limit(50)
            )).all()
            dialog = [(m.role, m.content) for m in rows]
            if llm_configured():
                plan.niche, plan.goal = await extract_plan_meta(
                    db, plan.user_id, plan.persona_key, dialog, session.title
                )
                account = await db.get(XhsAccount, plan.account_id) if plan.account_id else None
                transcript = "\n".join(f"{'用户' if r == 'user' else '教练'}：{c}" for r, c in dialog)
                prompt = f"下面是用户与你的咨询对话：\n\n{transcript}\n\n"
                if account:
                    prompt += (
                        f"账号：{account.nickname}（account_id={account.id}，当前粉丝 {account.followers}）。"
                        "请先用工具查看该账号已有的流量数据，再结合数据制定方案。\n"
                    )
                else:
                    prompt += "账号：全新账号，暂无数据。\n"
                prompt += "请基于以上信息，按起号方法论的 6 个部分输出完整方案，具体到每周的动作。"
                plan.content = await ask(db, plan.persona_key, prompt, plan.user_id)
            else:
                plan.niche = session.title[:64] or "起号"
                plan.goal = "AI 对话生成"
                plan.content = _template_plan(plan.niche, plan.goal)
            plan.status = "done"
        except Exception as exc:
            plan.status, plan.error = "failed", str(exc)[:1000]
        await db.commit()
