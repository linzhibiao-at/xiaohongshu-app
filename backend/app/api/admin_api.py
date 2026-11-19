"""Admin console API: operations (operator + admin) and ops/maintenance (admin)."""

import secrets
import string
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.plugins import load_all_skills, load_builtin_personas, load_builtin_skills
from app.agents.runtime import llm_configured
from app.api.user_api import AccountOut, PersonaIn, PlanOut, TaskOut, UserOut, _check_skills
from app.config import get_settings
from app.db import get_db
from app.deps import admin_user, audit, staff_user
from app.models import AuditLog, GrowthPlan, InteractionTask, Note, Persona, PluginState, User, XhsAccount
from app.redis_client import get_redis
from app.security import hash_password
from app.services.interaction import QUEUE_KEY
from app.services.skills import (
    SkillConflictError,
    SkillUploadError,
    bump_skills_version,
    delete_skill,
    parse_upload,
    read_uploads,
    save_skill,
)
from app.xhs.base import connector_names

router = APIRouter(prefix="/api/admin")


async def _count(db: AsyncSession, model, *where) -> int:
    return await db.scalar(select(func.count()).select_from(model).where(*where)) or 0


# ---------- dashboard ----------
@router.get("/stats")
async def stats(_: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    since = datetime.now() - timedelta(days=7)
    by_status = dict(
        (await db.execute(select(InteractionTask.status, func.count()).group_by(InteractionTask.status))).all()
    )
    return {
        "users": await _count(db, User, User.is_deleted.is_(False)),
        "new_users_7d": await _count(db, User, User.is_deleted.is_(False), User.created_at >= since),
        "xhs_accounts": await _count(db, XhsAccount),
        "notes": await _count(db, Note),
        "plans": await _count(db, GrowthPlan),
        "plans_failed": await _count(db, GrowthPlan, GrowthPlan.status == "failed"),
        "interaction_tasks": by_status,
        "queue_length": await get_redis().zcard(QUEUE_KEY),
    }


# ---------- users ----------
class UsersPage(BaseModel):
    items: list[UserOut]
    total: int
    page: int
    page_size: int


@router.get("/users", response_model=UsersPage)
async def users(
    q: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(10),
    _: User = Depends(staff_user),
    db: AsyncSession = Depends(get_db),
):
    if page_size not in (10, 20, 50):
        raise HTTPException(400, "每页条数仅支持 10 / 20 / 50")
    where = [User.is_deleted.is_(False)]
    if q:
        where.append(User.username.contains(q))
    stmt = (
        select(User).where(*where).order_by(User.id.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    return {
        "items": (await db.scalars(stmt)).all(),
        "total": await _count(db, User, *where),
        "page": page,
        "page_size": page_size,
    }


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[\w.@-]+$")
    password: str = Field(min_length=6, max_length=72)
    role: Literal["user", "operator", "admin"] = "user"


@router.post("/users", response_model=UserOut)
async def create_user(
    body: UserCreate, actor: User = Depends(staff_user), db: AsyncSession = Depends(get_db)
):
    """Staff-provisioned account; self-service registration is disabled."""
    if actor.role != "admin" and body.role != "user":
        raise HTTPException(403, "只有管理员可以创建运营或管理员账号")
    if await db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, "用户名已存在")
    target = User(username=body.username, password_hash=hash_password(body.password), role=body.role)
    db.add(target)
    audit(db, actor, "user.create", f"user={target.username} role={target.role}")
    await db.commit()
    await db.refresh(target)  # load server_default created_at (MySQL has no RETURNING)
    return target


class UserPatch(BaseModel):
    is_active: bool | None = None
    role: Literal["user", "operator", "admin"] | None = None


@router.patch("/users/{user_id}", response_model=UserOut)
async def patch_user(
    user_id: int, body: UserPatch, actor: User = Depends(staff_user), db: AsyncSession = Depends(get_db)
):
    """Toggle the status or change the role of an account."""
    target = await db.get(User, user_id)
    if target is None or target.is_deleted:
        raise HTTPException(404, "用户不存在")
    if target.id == actor.id:
        raise HTTPException(400, "不能修改自己的状态或角色")
    if actor.role != "admin" and (body.role is not None or target.role != "user"):
        raise HTTPException(403, "只有管理员可以修改角色或管理后台账号")
    if body.is_active is not None:
        target.is_active = body.is_active
    if body.role is not None:
        target.role = body.role
    audit(db, actor, "user.update", f"user={target.username} {body.model_dump(exclude_none=True)}")
    await db.commit()
    return target


def _random_numeric_password(length: int = 6) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(length))


class PasswordResetIn(BaseModel):
    # Empty or omitted -> a random 6-digit password is generated.
    password: str | None = Field(default=None, min_length=6, max_length=72)


@router.post("/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: int,
    body: PasswordResetIn | None = None,
    actor: User = Depends(staff_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset to the given password, or generate a random 6-digit one; returned once."""
    target = await db.get(User, user_id)
    if target is None or target.is_deleted:
        raise HTTPException(404, "用户不存在")
    if actor.role != "admin" and target.role != "user":
        raise HTTPException(403, "只有管理员可以重置运营或管理员账号的密码")
    chosen = body.password if body else None
    password = chosen or _random_numeric_password()
    target.password_hash = hash_password(password)
    audit(db, actor, "user.reset_password", f"user={target.username} id={target.id}")
    await db.commit()
    return {"password": password, "generated": not chosen}


async def _remove_users(actor: User, ids: list[int], db: AsyncSession) -> dict:
    """Soft delete: the accounts disappear, every related row is kept intact."""
    targets = (await db.scalars(select(User).where(User.id.in_(ids), User.is_deleted.is_(False)))).all()
    if not targets:
        raise HTTPException(404, "用户不存在")
    if any(t.id == actor.id for t in targets):
        raise HTTPException(400, "不能删除自己的账号")
    if actor.role != "admin" and any(t.role != "user" for t in targets):
        raise HTTPException(403, "只有管理员可以删除运营或管理员账号")
    for t in targets:
        t.is_deleted = True
        t.is_active = False
        audit(db, actor, "user.delete", f"user={t.username} id={t.id}")
    await db.commit()
    return {"deleted": len(targets)}


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, actor: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    return await _remove_users(actor, [user_id], db)


class UserIdsIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)


@router.post("/users/delete")
async def delete_users(body: UserIdsIn, actor: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    return await _remove_users(actor, body.ids, db)


@router.get("/users/{user_id}/accounts", response_model=list[AccountOut])
async def user_accounts(
    user_id: int, actor: User = Depends(staff_user), db: AsyncSession = Depends(get_db)
):
    """Xiaohongshu accounts bound to a user (read-only)."""
    target = await db.get(User, user_id)
    if target is None or target.is_deleted:
        raise HTTPException(404, "用户不存在")
    if actor.role != "admin" and target.role != "user":
        raise HTTPException(403, "只有管理员可以查看运营或管理员账号的绑定信息")
    stmt = select(XhsAccount).where(XhsAccount.user_id == user_id).order_by(XhsAccount.id.desc())
    return (await db.scalars(stmt)).all()


# ---------- content operations ----------
@router.get("/plans", response_model=list[PlanOut])
async def plans(status: str | None = None, _: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    stmt = select(GrowthPlan).order_by(GrowthPlan.id.desc()).limit(200)
    if status:
        stmt = stmt.where(GrowthPlan.status == status)
    return (await db.scalars(stmt)).all()


@router.get("/interactions", response_model=list[TaskOut])
async def interactions(status: str | None = None, _: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    stmt = select(InteractionTask).order_by(InteractionTask.id.desc()).limit(300)
    if status:
        stmt = stmt.where(InteractionTask.status == status)
    return (await db.scalars(stmt)).all()


@router.post("/interactions/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(task_id: int, actor: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    task = await db.get(InteractionTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status not in ("pending_approval", "queued"):
        raise HTTPException(400, "该任务已无法取消")
    task.status, task.error = "cancelled", "被运营人员取消"
    audit(db, actor, "interaction.cancel", f"task={task.id}")
    await db.commit()
    return task


# ---------- plugins ----------
@router.get("/plugins")
async def plugins(_: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    states = {(s.kind, s.name): s.enabled for s in await db.scalars(select(PluginState))}
    custom = (await db.scalars(select(Persona).order_by(Persona.id.desc()))).all()
    skills = await load_all_skills()
    return {
        "personas": [
            {"key": p.key, "name": p.name, "description": p.description, "skills": p.skills,
             "system_prompt": p.system_prompt, "enabled": states.get(("persona", p.key), True)}
            for p in load_builtin_personas().values()
        ],
        "skills": [
            {"name": s.name, "description": s.description, "has_tools": s.has_tools,
             "instructions": s.instructions, "enabled": states.get(("skill", s.name), True),
             "source": s.source, "file_count": s.file_count, "size": s.size}
            for s in skills.values()
        ],
        "custom_personas": [
            {"key": p.key, "name": p.name, "description": p.description, "skills": p.skills,
             "system_prompt": p.system_prompt, "owner_id": p.owner_id, "is_public": p.is_public}
            for p in custom
        ],
    }


class PluginToggle(BaseModel):
    kind: Literal["persona", "skill"]
    name: str = Field(max_length=64)
    enabled: bool


@router.post("/plugins/toggle")
async def toggle_plugin(body: PluginToggle, actor: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    known = load_builtin_personas() if body.kind == "persona" else await load_all_skills()
    if body.name not in known:
        raise HTTPException(404, "插件不存在")
    state = await db.scalar(select(PluginState).where(PluginState.kind == body.kind, PluginState.name == body.name))
    if state is None:
        state = PluginState(kind=body.kind, name=body.name)
        db.add(state)
    state.enabled = body.enabled
    audit(db, actor, "plugin.toggle", f"{body.kind}:{body.name} -> {body.enabled}")
    await db.commit()
    return {"ok": True}


@router.post("/skills")
async def upload_skill(
    files: list[UploadFile] = File(...),
    paths: list[str] = Form(...),
    overwrite: bool = Form(False),
    actor: User = Depends(staff_user),
    db: AsyncSession = Depends(get_db),
):
    """Store an uploaded skill folder in the DB; every pod picks it up on its next request."""
    try:
        contents = await read_uploads(files, paths)  # enforces the size limits while reading
        payload = parse_upload(paths, contents)
    except SkillUploadError as exc:
        raise HTTPException(400, str(exc)) from exc
    if payload.name in load_builtin_skills():
        raise HTTPException(409, "与内置技能重名")
    try:
        replaced = await save_skill(db, payload, created_by=actor.id, overwrite=overwrite)
        audit(db, actor, "skill.upload", f"{payload.name} files={payload.file_count} size={payload.total_size}")
        await db.commit()  # skill row, files and the audit entry land together
    except SkillConflictError as exc:
        raise HTTPException(409, "已存在同名技能，确认覆盖后重试") from exc
    await bump_skills_version()
    return {
        "name": payload.name,
        "file_count": payload.file_count,
        "total_size": payload.total_size,
        "replaced": replaced,
    }


@router.delete("/skills/{name}")
async def remove_skill(name: str, actor: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    if name in load_builtin_skills():
        raise HTTPException(400, "内置技能不能删除")
    if not await delete_skill(db, name):
        raise HTTPException(404, "技能不存在")
    state = await db.scalar(
        select(PluginState).where(PluginState.kind == "skill", PluginState.name == name)
    )
    if state is not None:
        await db.delete(state)
    audit(db, actor, "skill.delete", name)
    await db.commit()  # row, plugin state and audit together; notify afterwards
    await bump_skills_version()
    return {"deleted": 1}


class PublicPersonaIn(PersonaIn):
    key: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_]+$")


@router.post("/personas")
async def create_public_persona(
    body: PublicPersonaIn, actor: User = Depends(staff_user), db: AsyncSession = Depends(get_db)
):
    """Operator-published persona, visible to every user."""
    await _check_skills(db, body.skills)
    if body.key in load_builtin_personas() or await db.scalar(select(Persona).where(Persona.key == body.key)):
        raise HTTPException(409, "key 已存在")
    db.add(Persona(owner_id=None, is_public=True, **body.model_dump()))
    audit(db, actor, "persona.create", body.key)
    await db.commit()
    return {"key": body.key}


@router.delete("/personas/{key}")
async def delete_persona(key: str, actor: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    row = await db.scalar(select(Persona).where(Persona.key == key))
    if row is None:
        raise HTTPException(404, "人格不存在")
    await db.delete(row)
    audit(db, actor, "persona.delete", key)
    await db.commit()
    return {"ok": True}


# ---------- ops / maintenance (admin only) ----------
@router.get("/system")
async def system(_: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    s = get_settings()
    health = {}
    try:
        await db.execute(text("SELECT 1"))
        health["mysql"] = "ok"
    except Exception as exc:
        health["mysql"] = f"error: {exc}"
    r = get_redis()
    try:
        await r.ping()
        health["redis"] = "ok"
    except Exception as exc:
        health["redis"] = f"error: {exc}"
    heartbeat = await r.get("xhs:worker:heartbeat") if health["redis"] == "ok" else None
    return {
        "health": health,
        "worker_last_heartbeat": heartbeat,
        "queue_length": await r.zcard(QUEUE_KEY) if health["redis"] == "ok" else None,
        "llm": {"provider": s.llm_provider, "model": s.llm_model, "configured": llm_configured()},
        "sandbox_configured": bool(s.e2b_api_key),
        "xhs_connector": s.xhs_connector,
        "available_connectors": connector_names(),
        "limits": {
            "interaction_daily_limit": s.interaction_daily_limit,
            "interaction_min_interval_seconds": s.interaction_min_interval_seconds,
        },
    }


@router.get("/audit-logs")
async def audit_logs(_: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    rows = await db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(300))
    return [
        {"id": a.id, "actor_id": a.actor_id, "action": a.action, "detail": a.detail, "created_at": a.created_at}
        for a in rows
    ]
