"""C-end API (web + app)."""

import json
import re
import secrets
from datetime import datetime
from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.plugins import disabled_plugins, list_personas, load_all_skills
from app.agents.runtime import LLMNotConfigured, llm_configured
from app.auth.session import TicketStoreError, consume_ticket, issue_ticket, new_state, read_state
from app.auth.wechat import AUTHORIZE_URL, WeChatError, WeChatUser, build_provider
from app.config import get_settings
from app.db import SessionLocal, get_db
from app.deps import audit, current_user, owned_account
from app.models import (
    GrowthPlan,
    InteractionTask,
    Persona,
    PlanMessage,
    PlanSession,
    User,
    UserIdentity,
    XhsAccount,
)
from app.security import create_token, hash_password, verify_password
from app.services import agent_tasks, analytics, interaction, note_analysis
from app.xhs.base import ConnectorError, get_connector

router = APIRouter(prefix="/api")


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- auth ----------
class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[\w.@-]+$")
    password: str = Field(min_length=6, max_length=72)
    # "admin" rejects plain users at the door; the C-end always uses the default.
    scope: Literal["client", "admin"] = "client"


class UserOut(ORM):
    id: int
    username: str
    role: str
    is_active: bool
    created_at: datetime


@router.post("/auth/login")
async def login(body: Credentials, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.username == body.username))
    if user is None or user.is_deleted or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(403, "账号已被停用")
    if body.scope == "admin" and user.role == "user":
        raise HTTPException(403, "该账号没有管理后台权限")
    return {"token": create_token(user.id, user.role), "user": UserOut.model_validate(user)}


@router.get("/auth/me", response_model=UserOut)
async def me(user: User = Depends(current_user)):
    return user


# ---------- 微信登录（公众号网页授权）----------
LOGIN_PAGE = "/#/pages/login/login"


def _login_page(query: str) -> str:
    return f"{get_settings().client_base_url}{LOGIN_PAGE}?{query}"


@router.get("/auth/methods")
async def auth_methods():
    return {
        "password": True,
        "wechat": {"enabled": build_provider() is not None, "mode": get_settings().wechat_mode},
    }


@router.get("/auth/wechat/authorize")
async def wechat_authorize(request: Request):
    provider = build_provider()
    if provider is None:
        raise HTTPException(404, "微信登录未配置")
    settings = get_settings()
    state = new_state()
    # 反代（vite / nginx）必须保留 Host，回调地址才落在公众号配置的授权域名内。
    callback = f"{request.base_url}api/auth/wechat/callback"
    if settings.wechat_mode == "mock":
        return RedirectResponse(f"{callback}?code=mock_code&state={state}", status_code=302)
    url = (
        f"{AUTHORIZE_URL}?appid={quote(settings.wechat_app_id)}"
        f"&redirect_uri={quote(callback, safe='')}"
        f"&response_type=code&scope=snsapi_userinfo&state={state}#wechat_redirect"
    )
    return RedirectResponse(url, status_code=302)


@router.get("/auth/wechat/callback")
async def wechat_callback(code: str = "", state: str = "", db: AsyncSession = Depends(get_db)):
    provider = build_provider()
    if provider is None or not code or not read_state(state):
        return RedirectResponse(_login_page("error=wechat_failed"), status_code=302)
    try:
        user = await _wechat_login(db, await provider.exchange_code(code))
        ticket = await issue_ticket(user.id)
    except (WeChatError, TicketStoreError, httpx.HTTPError):
        # 微信侧故障/网络不可达/ticket 存储不可用：用户不应当停在裸 JSON 页，
        # 统一退回登录页，由前端提示重试（账密登录不受影响）。
        return RedirectResponse(_login_page("error=wechat_failed"), status_code=302)
    return RedirectResponse(_login_page(f"ticket={ticket}"), status_code=302)


class WechatTicketIn(BaseModel):
    ticket: str = Field(min_length=8, max_length=128)


@router.post("/auth/wechat/exchange")
async def wechat_exchange(body: WechatTicketIn, db: AsyncSession = Depends(get_db)):
    try:
        user_id = await consume_ticket(body.ticket)
    except TicketStoreError:
        raise HTTPException(503, "微信登录暂不可用，请使用账号密码登录") from None
    if user_id is None:
        raise HTTPException(401, "登录凭证已失效，请重新授权")
    user = await db.get(User, user_id)
    if user is None or user.is_deleted or not user.is_active:
        raise HTTPException(403, "账号已被停用")
    return {"token": create_token(user.id, user.role), "user": UserOut.model_validate(user)}


async def _find_identity(db: AsyncSession, openid: str) -> UserIdentity | None:
    return await db.scalar(
        select(UserIdentity).where(
            UserIdentity.provider == "wechat", UserIdentity.provider_uid == openid
        )
    )


async def _wechat_login(db: AsyncSession, wx: WeChatUser) -> User:
    """按 (provider, openid) 找账号；没有就建一个不能密码登录的新用户。

    不按昵称/手机号合并老账号：误匹配会导致账号冒领。
    """
    ident = await _find_identity(db, wx.openid)
    if ident is not None:
        user = await db.get(User, ident.user_id)
        if user is not None:
            if wx.nickname:
                ident.nickname = wx.nickname
            if wx.avatar:
                ident.avatar = wx.avatar
            if wx.unionid:
                ident.unionid = wx.unionid
            await db.commit()
            return user

    for _ in range(5):
        username = f"wx_{secrets.token_hex(4)}"
        if await db.scalar(select(User.id).where(User.username == username)) is None:
            break
    else:
        raise HTTPException(500, "创建微信账号失败，请重试")

    user = User(username=username, password_hash=hash_password(secrets.token_urlsafe(24)), role="user")
    db.add(user)
    try:
        await db.flush()
        db.add(
            UserIdentity(
                user_id=user.id,
                provider="wechat",
                provider_uid=wx.openid,
                unionid=wx.unionid,
                nickname=wx.nickname,
                avatar=wx.avatar,
            )
        )
        await db.commit()
    except IntegrityError:
        # 并发下同一 openid 可能刚被另一请求建号，回滚后按身份再查一次。
        await db.rollback()
        ident = await _find_identity(db, wx.openid)
        user = await db.get(User, ident.user_id) if ident is not None else None
        if user is None:
            raise
    return user


# ---------- xhs accounts ----------
class AccountIn(BaseModel):
    xhs_id: str = Field(min_length=1, max_length=64)
    nickname: str = Field(min_length=1, max_length=64)
    credential: str = ""
    auto_interaction: bool = False


class AccountOut(ORM):
    id: int
    xhs_id: str
    nickname: str
    niche: str
    followers: int
    connector: str
    auto_interaction: bool
    created_at: datetime


@router.get("/accounts", response_model=list[AccountOut])
async def list_accounts(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(XhsAccount).where(XhsAccount.user_id == user.id))).all()


@router.post("/accounts", response_model=AccountOut)
async def create_account(body: AccountIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    account = XhsAccount(user_id=user.id, connector=get_settings().xhs_connector, **body.model_dump())
    db.add(account)
    await db.commit()
    return account


@router.put("/accounts/{account_id}", response_model=AccountOut)
async def update_account(
    account_id: int, body: AccountIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    account = await owned_account(account_id, user, db)
    for k, v in body.model_dump().items():
        if k == "credential" and not v:
            continue  # empty means "keep the stored one"
        setattr(account, k, v)
    await db.commit()
    return account


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await db.delete(await owned_account(account_id, user, db))
    await db.commit()
    return {"ok": True}


# ---------- growth plans ----------
class PlanIn(BaseModel):
    niche: str = Field(min_length=1, max_length=64)
    goal: str = Field(min_length=1, max_length=255)
    account_id: int | None = None
    persona_key: str = "growth_coach"


class PlanOut(ORM):
    id: int
    niche: str
    goal: str
    account_id: int | None
    persona_key: str
    status: str
    content: str
    error: str
    created_at: datetime


@router.post("/plans", response_model=PlanOut)
async def create_plan(
    body: PlanIn, bg: BackgroundTasks, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    if body.account_id is not None:
        await owned_account(body.account_id, user, db)
    plan = GrowthPlan(user_id=user.id, **body.model_dump())
    db.add(plan)
    await db.commit()
    bg.add_task(agent_tasks.generate_plan, plan.id)
    return plan


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    q = select(GrowthPlan).where(GrowthPlan.user_id == user.id).order_by(GrowthPlan.id.desc())
    return (await db.scalars(q)).all()


@router.get("/plans/{plan_id}", response_model=PlanOut)
async def get_plan(plan_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    plan = await db.get(GrowthPlan, plan_id)
    if plan is None or plan.user_id != user.id:
        raise HTTPException(404, "方案不存在")
    return plan


# ---------- plan chat sessions ----------
class PlanSessionOut(BaseModel):
    id: int
    title: str
    account_id: int | None
    updated_at: datetime
    plan_id: int | None = None


class PlanMessageOut(ORM):
    id: int
    role: str
    content: str
    plan_id: int | None
    created_at: datetime


class PlanSessionDetail(BaseModel):
    session: PlanSessionOut
    messages: list[PlanMessageOut]


async def _own_session(session_id: int, user: User, db: AsyncSession) -> PlanSession:
    session = await db.get(PlanSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(404, "会话不存在")
    return session


@router.get("/plan-sessions", response_model=list[PlanSessionOut])
async def list_plan_sessions(
    limit: int = 50, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    limit = min(max(limit, 1), 100)
    rows = (
        await db.scalars(
            select(PlanSession)
            .where(PlanSession.user_id == user.id)
            .order_by(PlanSession.updated_at.desc())
            .limit(limit)
        )
    ).all()
    latest = dict(
        (
            await db.execute(
                select(PlanMessage.session_id, func.max(PlanMessage.plan_id))
                .where(PlanMessage.session_id.in_([r.id for r in rows]), PlanMessage.plan_id.is_not(None))
                .group_by(PlanMessage.session_id)
            )
        ).all()
    )
    return [
        PlanSessionOut(
            id=r.id, title=r.title, account_id=r.account_id, updated_at=r.updated_at, plan_id=latest.get(r.id)
        )
        for r in rows
    ]


@router.get("/plan-sessions/{session_id}", response_model=PlanSessionDetail)
async def get_plan_session(
    session_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    session = await _own_session(session_id, user, db)
    msgs = (
        await db.scalars(
            select(PlanMessage)
            .where(PlanMessage.session_id == session.id)
            .order_by(PlanMessage.id.desc())
            .limit(200)
        )
    ).all()
    plan_id = await db.scalar(
        select(func.max(PlanMessage.plan_id)).where(
            PlanMessage.session_id == session.id, PlanMessage.plan_id.is_not(None)
        )
    )
    return {
        "session": PlanSessionOut(
            id=session.id, title=session.title, account_id=session.account_id,
            updated_at=session.updated_at, plan_id=plan_id,
        ),
        "messages": list(reversed(msgs)),
    }


@router.delete("/plan-sessions/{session_id}")
async def delete_plan_session(
    session_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    session = await _own_session(session_id, user, db)
    await db.execute(delete(PlanMessage).where(PlanMessage.session_id == session.id))
    await db.delete(session)
    await db.commit()
    return {"ok": True}


class SessionChatIn(BaseModel):
    session_id: int | None = None
    account_id: int | None = None
    message: str = Field(min_length=1, max_length=4000)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/plan-sessions/chat")
async def plan_session_chat(
    body: SessionChatIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Stream a reply token-by-token over SSE; the request session is not used inside the stream."""
    if not llm_configured():
        raise HTTPException(503, "AI 功能未启用：未配置 LLM_API_KEY")
    if body.session_id is not None:
        await _own_session(body.session_id, user, db)
    elif body.account_id is not None:
        await owned_account(body.account_id, user, db)

    async def gen():
        async with SessionLocal() as sdb:
            try:
                if body.session_id is None:
                    session = PlanSession(
                        user_id=user.id, account_id=body.account_id,
                        title=" ".join(body.message.split())[:30],
                    )
                    sdb.add(session)
                    await sdb.commit()
                    await sdb.refresh(session)
                else:
                    session = await sdb.get(PlanSession, body.session_id)
                    if session is None:
                        return
                account = await sdb.get(XhsAccount, session.account_id) if session.account_id else None
                history = [
                    (m.role, m.content)
                    for m in (await sdb.scalars(
                        select(PlanMessage)
                        .where(PlanMessage.session_id == session.id, PlanMessage.plan_id.is_(None))
                        .order_by(PlanMessage.id.desc())
                        .limit(20)
                    )).all()
                ][::-1]
                user_msg = PlanMessage(session_id=session.id, role="user", content=body.message)
                sdb.add(user_msg)
                session.updated_at = datetime.now()
                await sdb.commit()
                await sdb.refresh(user_msg)
                yield _sse("session", {
                    "session_id": session.id, "title": session.title,
                    "user_message": PlanMessageOut.model_validate(user_msg).model_dump(mode="json"),
                })
                parts = []
                async for delta in agent_tasks.chat_in_session_stream(
                    sdb, user.id, session.persona_key, account, history, body.message
                ):
                    parts.append(delta)
                    yield _sse("delta", {"text": delta})
                reply = PlanMessage(session_id=session.id, role="assistant", content="".join(parts))
                sdb.add(reply)
                session.updated_at = datetime.now()
                await sdb.commit()
                await sdb.refresh(reply)
                yield _sse("done", {"message": PlanMessageOut.model_validate(reply).model_dump(mode="json")})
            except Exception as exc:
                yield _sse("error", {"detail": f"AI 调用失败: {exc}"})

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/plan-sessions/{session_id}/plan")
async def generate_plan_from_session(
    session_id: int, bg: BackgroundTasks,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
):
    session = await _own_session(session_id, user, db)
    user_msgs = await db.scalar(
        select(func.count()).select_from(PlanMessage)
        .where(PlanMessage.session_id == session.id, PlanMessage.role == "user")
    )
    if not user_msgs:
        raise HTTPException(400, "先聊几句再生成")
    last_plan_id = await db.scalar(
        select(func.max(PlanMessage.plan_id))
        .where(PlanMessage.session_id == session.id, PlanMessage.plan_id.is_not(None))
    )
    if last_plan_id:
        last = await db.get(GrowthPlan, last_plan_id)
        if last is not None and last.status in ("pending", "running"):
            raise HTTPException(400, "方案生成中，请稍候")
    plan = GrowthPlan(
        user_id=user.id, account_id=session.account_id,
        niche=(session.title or "起号")[:64], goal="AI 对话生成",
        persona_key=session.persona_key,
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    db.add(PlanMessage(session_id=session.id, role="assistant", content="", plan_id=plan.id))
    session.updated_at = datetime.now()
    await db.commit()
    bg.add_task(agent_tasks.generate_plan_from_session, plan.id, session.id)
    return {"plan_id": plan.id}


# ---------- public note analysis ----------
# Open to visitors on purpose: it reads one public note and returns a rule-based
# read-out, no persistence and no LLM call.
class NoteLinkIn(BaseModel):
    url: str = Field(min_length=1, max_length=512)


@router.post("/notes/analyze")
async def analyze_note_link(body: NoteLinkIn):
    try:
        note_ref = note_analysis.extract_note_ref(body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        note = await get_connector(get_settings().xhs_connector).get_note(note_ref)
    except ConnectorError as exc:
        raise HTTPException(502, str(exc)) from exc
    return note_analysis.analyze(note)


# ---------- traffic analytics ----------
@router.post("/accounts/{account_id}/sync")
async def sync_notes(account_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    account = await owned_account(account_id, user, db)
    try:
        return {"synced": await analytics.sync_account(db, account)}
    except Exception as exc:
        raise HTTPException(502, f"同步失败: {exc}") from exc


@router.get("/accounts/{account_id}/analytics")
async def get_analytics(account_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await owned_account(account_id, user, db)
    return await analytics.account_report(db, account_id)


@router.post("/accounts/{account_id}/analytics/diagnose")
async def diagnose(account_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    account = await owned_account(account_id, user, db)
    report = await analytics.account_report(db, account_id)
    if not report["note_count"]:
        raise HTTPException(400, "请先同步笔记数据")
    return {"diagnosis": await _agent_call(agent_tasks.diagnose(db, user.id, account, report))}


async def _agent_call(coro):
    try:
        return await coro
    except LLMNotConfigured as exc:
        raise HTTPException(503, f"AI 功能未启用：{exc}") from exc
    except agent_tasks.PersonaUnavailable as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"AI 调用失败: {exc}") from exc


# ---------- interactions ----------
class InteractionIn(BaseModel):
    account_id: int
    target_note: str = Field(min_length=1, max_length=512)
    actions: list[Literal["like", "collect", "comment"]] = Field(min_length=1)
    comment_text: str = Field(default="", max_length=1000)
    # When commenting without text: let the agent draft it from this context.
    note_context: str = Field(default="", max_length=4000)


class TaskOut(ORM):
    id: int
    account_id: int
    target_note: str
    action: str
    comment_text: str
    status: str
    error: str
    created_at: datetime
    executed_at: datetime | None


@router.post("/interactions", response_model=list[TaskOut])
async def create_interactions(
    body: InteractionIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    account = await owned_account(body.account_id, user, db)
    comment_text = body.comment_text.strip()
    if "comment" in body.actions and not comment_text:
        if not body.note_context.strip():
            raise HTTPException(400, "评论需要填写评论内容，或提供笔记内容让 AI 起草")
        comment_text = await _agent_call(agent_tasks.draft_comment(db, user.id, body.note_context))
        if not comment_text:
            raise HTTPException(503, "AI 功能未启用，请手动填写评论内容")
    tasks = []
    for action in dict.fromkeys(body.actions):
        tasks.append(
            await interaction.create_task(
                db, account, body.target_note.strip(), action, comment_text if action == "comment" else ""
            )
        )
    return tasks


@router.get("/interactions", response_model=list[TaskOut])
async def list_interactions(
    status: str | None = None, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    q = select(InteractionTask).where(InteractionTask.user_id == user.id)
    if status:
        q = q.where(InteractionTask.status == status)
    return (await db.scalars(q.order_by(InteractionTask.id.desc()).limit(200))).all()


async def _own_task(task_id: int, user: User, db: AsyncSession) -> InteractionTask:
    task = await db.get(InteractionTask, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(404, "任务不存在")
    return task


class ApproveIn(BaseModel):
    comment_text: str | None = Field(default=None, max_length=1000)


@router.post("/interactions/{task_id}/approve", response_model=TaskOut)
async def approve(
    task_id: int, body: ApproveIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    task = await _own_task(task_id, user, db)
    if task.status != "pending_approval":
        raise HTTPException(400, "该任务不在待确认状态")
    await interaction.approve_task(db, task, body.comment_text)
    return task


@router.post("/interactions/{task_id}/cancel", response_model=TaskOut)
async def cancel(task_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    task = await _own_task(task_id, user, db)
    if task.status not in ("pending_approval", "queued"):
        raise HTTPException(400, "该任务已无法取消")
    task.status = "rejected" if task.status == "pending_approval" else "cancelled"
    await db.commit()
    return task


@router.get("/accounts/{account_id}/interaction-quota")
async def quota(account_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await owned_account(account_id, user, db)
    s = get_settings()
    return {
        "used_today": await interaction.usage_today(account_id),
        "daily_limit": s.interaction_daily_limit,
        "min_interval_seconds": s.interaction_min_interval_seconds,
    }


# ---------- personas & skills ----------
class PersonaIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=255)
    system_prompt: str = Field(min_length=10, max_length=8000)
    skills: list[str] = []


def _persona_out(p) -> dict:
    return {
        "key": p.key, "name": p.name, "description": p.description, "skills": p.skills,
        "builtin": p.builtin, "is_public": p.is_public, "system_prompt": p.system_prompt,
    }


@router.get("/personas")
async def personas(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return [
        {**_persona_out(p), "mine": p.owner_id == user.id} for p in await list_personas(db, user.id)
    ]


async def _check_skills(db: AsyncSession, names: list[str]) -> None:
    unknown = set(names) - set(await load_all_skills())
    if unknown:
        raise HTTPException(400, f"未知的 skill: {', '.join(sorted(unknown))}")


@router.post("/personas")
async def create_persona(body: PersonaIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await _check_skills(db, body.skills)
    slug = re.sub(r"\W+", "_", body.name).strip("_").lower() or "persona"
    row = Persona(key=f"u{user.id}_{slug}_{int(datetime.now().timestamp())}", owner_id=user.id, **body.model_dump())
    db.add(row)
    await db.commit()
    return {"key": row.key}


@router.put("/personas/{key}")
async def update_persona(
    key: str, body: PersonaIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    row = await _own_persona(key, user, db)
    await _check_skills(db, body.skills)
    for k, v in body.model_dump().items():
        setattr(row, k, v)
    await db.commit()
    return {"ok": True}


@router.delete("/personas/{key}")
async def delete_persona(key: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await db.delete(await _own_persona(key, user, db))
    await db.commit()
    return {"ok": True}


async def _own_persona(key: str, user: User, db: AsyncSession) -> Persona:
    row = await db.scalar(select(Persona).where(Persona.key == key))
    if row is None or row.owner_id != user.id:
        raise HTTPException(404, "人格不存在或不可修改")
    return row


@router.get("/skills")
async def skills(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    disabled = await disabled_plugins(db, "skill")
    return [
        {"name": s.name, "description": s.description, "has_tools": s.has_tools, "source": s.source}
        for s in (await load_all_skills()).values()
        if s.name not in disabled
    ]


# ---------- agent chat ----------
class ChatIn(BaseModel):
    persona_key: str = "growth_coach"
    message: str = Field(min_length=1, max_length=4000)


@router.post("/agent/chat")
async def chat(body: ChatIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    reply = await _agent_call(agent_tasks.chat(db, user.id, body.persona_key, body.message))
    return {"reply": reply}


@router.get("/agent/status")
async def agent_status(_: User = Depends(current_user)):
    return {"llm_configured": llm_configured(), "sandbox_configured": bool(get_settings().e2b_api_key)}
