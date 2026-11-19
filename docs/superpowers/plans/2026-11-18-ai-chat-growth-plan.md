# 起号方案 AI 对话（SSE 流式）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 client「方案」tab 从表单改为与小红书起号专家的 SSE 流式对话，会话落库可回看，聊够了一键生成正式起号方案。

**Architecture:** 新增 `plan_sessions` / `plan_messages` 两张表承载对话；`POST /api/plan-sessions/chat` 用 `Agent.reply_stream` 逐字推送 `text/event-stream`；`POST /api/plan-sessions/{id}/plan` 占位建 `GrowthPlan` 后在后台任务里提取元信息并生成方案，聊天中插入方案卡片消息。前端 H5 用 `fetch` 读流，App 端降级整包回放。

**Tech Stack:** FastAPI + SQLAlchemy async (MySQL/SQLite) + AgentScope 2.0.11 + uni-app Vue3。

**Spec:** `docs/superpowers/specs/2026-11-18-ai-chat-growth-plan-design.md`

## Global Constraints

- 工作区**不是 git 仓库**，计划中不含 commit 步骤；每个任务以测试全绿为检查点。
- 后端一律 `cd backend && uv run ...`；pytest 依赖 `pytest.ini` 的 `pythonpath = .`。
- 测试环境 `LLM_API_KEY=""`，所有涉及 LLM 的端点测试必须 monkeypatch。
- 不修改 `growth_coach.yaml`；对话约定拼在用户消息前缀里。
- 不删除或改动 `POST /api/plans`、`GET /api/plans`、`detail.vue`、admin 方案管理。
- 新增表只用 `create_all`（`init_db`），不改旧表结构。
- 流式端点内不得使用请求级 `get_db` 会话，必须自建 `SessionLocal()`。
- 所有时间字段使用应用侧默认值（`datetime.now`），禁止只依赖 `server_default`。

## Review Focus

1. **LLM 中途失败**：流已开始后抛错必须发 `error` 帧，用户消息保留、assistant 不落库，不出现空白 assistant 消息。
2. **流式与 DB 会话生命周期**：流式 generator 若复用请求级会话会因依赖提前关闭而报错，必须 `SessionLocal()`。
3. **跨用户访问**：会话列表只含本人；他人会话的 GET/DELETE/生成方案返回 404，不泄露存在性。
4. **生成方案的前置条件**：无用户消息返回 400；已有 `pending|running` 方案再点返回 400。
5. **前端中断与降级**：`onUnload` 必须 abort 流并清定时器；无 `fetch` 环境走整包解析，不抛未捕获异常。

---

### Task 1: 会话数据模型

**Files:**
- Modify: `backend/app/models.py`（在 `GrowthPlan` 之后追加两个类）
- Test: `backend/tests/test_plan_chat.py`（新建）

**Interfaces:**
- Consumes: `app.db.Base`、`TimestampMixin`、`datetime`、`mapped_column`（models.py 已导入）。
- Produces:
  - `PlanSession(id, user_id, account_id, title, persona_key, updated_at, created_at)`
  - `PlanMessage(id, session_id, role, content, plan_id, created_at)`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_plan_chat.py
from datetime import datetime

from sqlalchemy import select

from app.db import SessionLocal
from app.models import PlanMessage, PlanSession


async def test_plan_session_models(create_user):
    user_id, _ = await create_user("chat_owner")
    async with SessionLocal() as db:
        session = PlanSession(user_id=user_id, title="健身起号", updated_at=datetime.now())
        db.add(session)
        await db.commit()
        await db.refresh(session)
        db.add(PlanMessage(session_id=session.id, role="user", content="我想做健身"))
        await db.commit()
        rows = (await db.scalars(select(PlanMessage).where(PlanMessage.session_id == session.id))).all()
        assert session.title == "健身起号"
        assert session.persona_key == "growth_coach"
        assert session.updated_at is not None
        assert [(m.role, m.content) for m in rows] == [("user", "我想做健身")]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_plan_chat.py::test_plan_session_models -v`
Expected: FAIL（`ImportError: cannot import name 'PlanSession'`）

- [ ] **Step 3: 在 `backend/app/models.py` 的 `GrowthPlan` 之后添加**

```python
class PlanSession(Base, TimestampMixin):
    """One consultation thread with the growth coach."""

    __tablename__ = "plan_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("xhs_accounts.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(64), default="")
    persona_key: Mapped[str] = mapped_column(String(64), default="growth_coach")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class PlanMessage(Base, TimestampMixin):
    """A chat turn; plan_id non-null marks a generated-plan card."""

    __tablename__ = "plan_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("plan_sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("growth_plans.id"), nullable=True)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_plan_chat.py::test_plan_session_models -v`
Expected: PASS

---

### Task 2: 会话列表 / 详情 / 删除 API

**Files:**
- Modify: `backend/app/api/user_api.py`（在 growth plans 段之后新增 `# ---------- plan chat sessions ----------` 段）
- Test: `backend/tests/test_plan_chat.py`

**Interfaces:**
- Consumes: `PlanSession`、`PlanMessage`（Task 1）；`current_user`、`get_db`；`ORM`、`BaseModel`。
- Produces:
  - `GET /api/plan-sessions -> list[PlanSessionOut]`，`PlanSessionOut = {id, title, account_id, updated_at, plan_id}`
  - `GET /api/plan-sessions/{id} -> {session: PlanSessionOut, messages: list[PlanMessageOut]}`
  - `DELETE /api/plan-sessions/{id} -> {"ok": True}`
  - 内部助手 `_own_session(session_id, user, db) -> PlanSession`（404 语义）

- [ ] **Step 1: 写失败测试**

```python
# 追加到 backend/tests/test_plan_chat.py
from app.models import GrowthPlan


async def _seed_session(user_id: int, title: str = "健身起号", with_plan: bool = False):
    async with SessionLocal() as db:
        session = PlanSession(user_id=user_id, title=title, updated_at=datetime.now())
        db.add(session)
        await db.commit()
        await db.refresh(session)
        db.add(PlanMessage(session_id=session.id, role="user", content="我想做健身"))
        if with_plan:
            plan = GrowthPlan(user_id=user_id, niche=title, goal="涨粉", content="x", status="done")
            db.add(plan)
            await db.commit()
            await db.refresh(plan)
            db.add(PlanMessage(session_id=session.id, role="assistant", content="", plan_id=plan.id))
        await db.commit()
        return session.id, (plan.id if with_plan else None)


async def test_session_list_and_detail(client, create_user):
    user_id, headers = await create_user("chat_list")
    sid, _ = await _seed_session(user_id)
    listed = (await client.get("/api/plan-sessions", headers=headers)).json()
    assert [(s["id"], s["title"], s["plan_id"]) for s in listed] == [(sid, "健身起号", None)]
    detail = (await client.get(f"/api/plan-sessions/{sid}", headers=headers)).json()
    assert detail["session"]["id"] == sid
    assert [m["role"] for m in detail["messages"]] == ["user"]


async def test_session_plan_id_and_delete(client, create_user):
    user_id, headers = await create_user("chat_planid")
    sid, plan_id = await _seed_session(user_id, with_plan=True)
    listed = (await client.get("/api/plan-sessions", headers=headers)).json()
    assert listed[0]["plan_id"] == plan_id
    assert (await client.delete(f"/api/plan-sessions/{sid}", headers=headers)).json() == {"ok": True}
    assert (await client.get("/api/plan-sessions", headers=headers)).json() == []
    assert (await client.get("/api/plan-sessions/{sid}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/plans/{plan_id}", headers=headers)).status_code == 200


async def test_session_foreign_access_404(client, create_user):
    _, other_headers = await create_user("chat_other")
    owner_id, owner_headers = await create_user("chat_owner2")
    sid, _ = await _seed_session(owner_id)
    assert (await client.get(f"/api/plan-sessions/{sid}", headers=other_headers)).status_code == 404
    assert (await client.get("/api/plan-sessions", headers=other_headers)).json() == []
    assert (await client.delete(f"/api/plan-sessions/{sid}", headers=other_headers)).status_code == 404
```

同时每个测试都要 monkeypatch 掉 `llm_configured` 吗？不需要——这三个端点不调 LLM。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_plan_chat.py -v -k "list or delete or foreign"`
Expected: FAIL（404 / 405）

- [ ] **Step 3: 实现接口**

在 `user_api.py` 顶部补 `from sqlalchemy import delete, func, select`（现在只有 `select`），并新增：

```python
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
        (await db.execute(
            select(PlanMessage.session_id, func.max(PlanMessage.plan_id))
            .where(PlanMessage.session_id.in_([r.id for r in rows]), PlanMessage.plan_id.is_not(None))
            .group_by(PlanMessage.session_id)
        )).all()
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
    return {
        "session": PlanSessionOut(
            id=session.id, title=session.title, account_id=session.account_id,
            updated_at=session.updated_at,
            plan_id=await db.scalar(
                select(func.max(PlanMessage.plan_id)).where(
                    PlanMessage.session_id == session.id, PlanMessage.plan_id.is_not(None)
                )
            ),
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
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_plan_chat.py -v`
Expected: PASS（4 个测试）

---

### Task 3: 流式聊天端点（SSE）

**Files:**
- Modify: `backend/app/agents/runtime.py`（`_build_model` 加 `stream` 参数；新增 `run_agent_stream`）
- Modify: `backend/app/services/agent_tasks.py`（新增 `DIALOG_RULES`、`chat_in_session_stream`）
- Modify: `backend/app/api/user_api.py`（新增 `POST /api/plan-sessions/chat`）
- Test: `backend/tests/test_plan_chat.py`

**Interfaces:**
- Consumes: Task 1/2 的模型与 `_own_session`；`owned_account`；`llm_configured`；`SessionLocal`。
- Produces:
  - `run_agent_stream(persona, skills, prompt, user_id=None) -> AsyncIterator[str]`
  - `chat_in_session_stream(db, user_id, persona_key, account, history, message) -> AsyncIterator[str]`，`history: list[tuple[str, str]]`
  - SSE 帧：`session` / `delta` / `done` / `error`

- [ ] **Step 1: 写失败测试**

```python
# 追加到 backend/tests/test_plan_chat.py
import json

import pytest

from app.services import agent_tasks


async def _collect_sse(client, headers, payload):
    frames = []
    async with client.stream("POST", "/api/plan-sessions/chat", headers=headers, json=payload) as res:
        assert res.status_code == 200, await res.aread()
        raw = "".join([chunk async for chunk in res.aiter_text()])
    for block in [b for b in raw.split("\n\n") if b.strip()]:
        event, data = "message", ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        frames.append((event, json.loads(data) if data else {}))
    return frames


@pytest.fixture
def fake_stream(monkeypatch):
    async def _fake(db, user_id, persona_key, account, history, message):
        assert message
        yield "你好"
        yield "，先聊聊赛道？"

    monkeypatch.setattr(agent_tasks, "chat_in_session_stream", _fake)
    monkeypatch.setattr("app.api.user_api.llm_configured", lambda: True)


async def test_chat_stream_creates_session(client, create_user, fake_stream):
    _, headers = await create_user("chat_stream")
    frames = await _collect_sse(client, headers, {"message": "我想做健身，30 天涨粉 1000"})
    events = [e for e, _ in frames]
    assert events[0] == "session" and events[-1] == "done"
    assert "".join(d["text"] for e, d in frames if e == "delta") == "你好，先聊聊赛道？"
    sid = frames[0][1]["session_id"]
    detail = (await client.get(f"/api/plan-sessions/{sid}", headers=headers)).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["session"]["title"] == "我想做健身，30 天涨粉 1000"[:30]
    assert detail["messages"][1]["content"] == "你好，先聊聊赛道？"


async def test_chat_stream_continues_session(client, create_user, fake_stream):
    user_id, headers = await create_user("chat_continue")
    sid, _ = await _seed_session(user_id)
    frames = await _collect_sse(client, headers, {"session_id": sid, "message": "每周能投入 5 小时"})
    assert frames[0][1]["session_id"] == sid
    detail = (await client.get(f"/api/plan-sessions/{sid}", headers=headers)).json()
    assert len(detail["messages"]) == 3


async def test_chat_stream_foreign_session_404(client, create_user):
    _, other = await create_user("chat_stream_other")
    owner_id, _ = await create_user("chat_stream_owner")
    sid, _ = await _seed_session(owner_id)
    async with client.stream("POST", "/api/plan-sessions/chat", headers=other, json={"session_id": sid, "message": "hi"}) as res:
        assert res.status_code == 404


async def test_chat_stream_requires_llm(client, create_user):
    # conftest 已指定 LLM_API_KEY=""，无需 mock
    _, headers = await create_user("chat_no_llm")
    async with client.stream("POST", "/api/plan-sessions/chat", headers=headers, json={"message": "hi"}) as res:
        assert res.status_code == 503
```

去掉测试里那行中文伪代码，改为注释。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_plan_chat.py -v -k "stream"`
Expected: FAIL（404，端点不存在）

- [ ] **Step 3: 实现**

`runtime.py`：
1. `_build_model()` 改为 `_build_model(stream: bool = False)`，三个 provider 分支的 `stream=False` 改为 `stream=stream`。
2. 新增：

```python
async def run_agent_stream(
    persona: PersonaSpec, skills: list[SkillSpec], prompt: str, user_id: int | None = None
) -> AsyncIterator[str]:
    from agentscope.agent import Agent, ReActConfig
    from agentscope.event import TextBlockDeltaEvent
    from agentscope.message import UserMsg
    from agentscope.tool import Toolkit

    toolkit = Toolkit()
    await _register_skill_tools(toolkit, skills)
    agent = Agent(
        name=persona.name,
        system_prompt=build_system_prompt(persona, skills),
        model=_build_model(stream=True),
        toolkit=toolkit,
        react_config=ReActConfig(max_iters=8),
    )
    token = current_user_id.set(user_id)
    try:
        async for evt in agent.reply_stream(UserMsg(name="user", content=prompt)):
            if isinstance(evt, TextBlockDeltaEvent):
                yield evt.delta
    finally:
        current_user_id.reset(token)
```

`agent_tasks.py`：

```python
DIALOG_RULES = (
    "\n\n# 对话约定\n"
    "- 信息不足时一次只问最关键的一个问题（赛道、目标、账号现状、每周可投入时间）。\n"
    "- 不要在对话里输出完整长方案；信息足够时提示用户点击下方「生成起号方案」。\n"
    "- 保持简洁，每条回复不超过 200 字（用户明确要求展开时除外）。"
)


async def chat_in_session_stream(
    db: AsyncSession, user_id: int, persona_key: str, account: XhsAccount | None,
    history: list[tuple[str, str]], message: str,
):
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
```

`user_api.py`：顶部补 `import json`；`from fastapi.responses import StreamingResponse`；`from app.db import SessionLocal, get_db`；models 补 `PlanMessage, PlanSession`；`sqlalchemy` 补 `delete, func`。

```python
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
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_plan_chat.py -v -k "stream"`
Expected: PASS（4 个）

- [ ] **Step 5: 回归**

Run: `cd backend && uv run pytest -q`
Expected: 全部通过（原 12 + 新增）

---

### Task 4: 从对话生成方案

**Files:**
- Modify: `backend/app/services/agent_tasks.py`（`extract_plan_meta`、`generate_plan_from_session`，models 导入补 `PlanMessage, PlanSession`、`from sqlalchemy import select`、`import re`）
- Modify: `backend/app/api/user_api.py`（`POST /api/plan-sessions/{id}/plan`）
- Test: `backend/tests/test_plan_chat.py`

**Interfaces:**
- Consumes: Task 1/2/3；`ask`、`run_agent`、`llm_configured`、`_template_plan`、`SessionLocal`。
- Produces:
  - `extract_plan_meta(db, user_id, persona_key, dialog, fallback_niche) -> tuple[str, str]`
  - `generate_plan_from_session(plan_id: int, session_id: int) -> None`
  - `POST /api/plan-sessions/{id}/plan -> {"plan_id": int}`

- [ ] **Step 1: 写失败测试**

```python
# 追加到 backend/tests/test_plan_chat.py
async def test_generate_plan_from_session_templates(client, create_user):
    user_id, headers = await create_user("chat_gen")
    sid, _ = await _seed_session(user_id)
    r = await client.post(f"/api/plan-sessions/{sid}/plan", headers=headers)
    assert r.status_code == 200
    plan_id = r.json()["plan_id"]
    plan = (await client.get(f"/api/plans/{plan_id}", headers=headers)).json()
    assert plan["status"] == "done" and plan["content"]
    detail = (await client.get(f"/api/plan-sessions/{sid}", headers=headers)).json()
    assert detail["messages"][-1]["plan_id"] == plan_id
    listed = (await client.get("/api/plan-sessions", headers=headers)).json()
    assert listed[0]["plan_id"] == plan_id


async def test_generate_plan_uses_extracted_meta(client, create_user, monkeypatch):
    user_id, headers = await create_user("chat_meta")
    sid, _ = await _seed_session(user_id, title="占位标题")

    async def _meta(db, user_id, persona_key, dialog, fallback_niche):
        return "健身", "30天涨粉1000"

    async def _ask(db, persona_key, prompt, user_id):
        return "方案正文"

    monkeypatch.setattr(agent_tasks, "llm_configured", lambda: True)
    monkeypatch.setattr(agent_tasks, "extract_plan_meta", _meta)
    monkeypatch.setattr(agent_tasks, "ask", _ask)
    plan_id = (await client.post(f"/api/plan-sessions/{sid}/plan", headers=headers)).json()["plan_id"]
    plan = (await client.get(f"/api/plans/{plan_id}", headers=headers)).json()
    assert (plan["niche"], plan["goal"], plan["content"]) == ("健身", "30天涨粉1000", "方案正文")


async def test_generate_requires_user_message(client, create_user):
    user_id, headers = await create_user("chat_empty")
    async with SessionLocal() as db:
        session = PlanSession(user_id=user_id, title="空会话", updated_at=datetime.now())
        db.add(session)
        await db.commit()
        await db.refresh(session)
        sid = session.id
    assert (await client.post(f"/api/plan-sessions/{sid}/plan", headers=headers)).status_code == 400


async def test_generate_conflict_while_running(client, create_user):
    user_id, headers = await create_user("chat_running")
    async with SessionLocal() as db:
        session = PlanSession(user_id=user_id, title="进行中", updated_at=datetime.now())
        db.add(session)
        await db.commit()
        await db.refresh(session)
        db.add(PlanMessage(session_id=session.id, role="user", content="hi"))
        plan = GrowthPlan(user_id=user_id, niche="x", goal="y", status="running")
        db.add(plan)
        await db.commit()
        await db.refresh(plan)
        db.add(PlanMessage(session_id=session.id, role="assistant", content="", plan_id=plan.id))
        await db.commit()
        sid = session.id
    r = await client.post(f"/api/plan-sessions/{sid}/plan", headers=headers)
    assert r.status_code == 400 and "生成中" in r.json()["detail"]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_plan_chat.py -v -k "generate"`
Expected: FAIL（404 / 405）

- [ ] **Step 3: 实现**

`agent_tasks.py`：

```python
async def extract_plan_meta(
    db: AsyncSession, user_id: int, persona_key: str,
    dialog: list[tuple[str, str]], fallback_niche: str,
) -> tuple[str, str]:
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
```

`user_api.py`：

```python
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
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/test_plan_chat.py -v && cd backend && uv run pytest -q`
Expected: 全部 PASS（含原 12 个）

---

### Task 5: 前端流式请求封装

**Files:**
- Modify: `client/src/api.js`（文件末尾新增 `streamChat`）

**Interfaces:**
- Consumes: `BASE`、`getToken` 与现有 request 错误语义。
- Produces: `streamChat(body, { onSession, onDelta, onDone, onError, signal }) -> Promise<void>`，`body = {session_id?, account_id?, message}`。

- [ ] **Step 1: 实现 `streamChat`**

```js
// Server-sent events for the plan chat. H5 streams via fetch; App/mini-program
// buffers the whole SSE body with uni.request and replays the frames at once.
export async function streamChat(body, { onSession, onDelta, onDone, onError, signal } = {}) {
  const url = `${BASE}/api/plan-sessions/chat`
  const headers = { 'Content-Type': 'application/json', Authorization: getToken() ? `Bearer ${getToken()}` : '' }
  const dispatch = (event, data) => {
    if (event === 'session') onSession && onSession(data)
    else if (event === 'delta') onDelta && onDelta(data.text || '')
    else if (event === 'done') onDone && onDone(data)
    else if (event === 'error') onError && onError(new Error(data.detail || 'AI 调用失败'))
  }
  const parse = (block) => {
    let event = 'message'
    const lines = []
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) lines.push(line.slice(5).trim())
    }
    if (!lines.length) return
    try { dispatch(event, JSON.parse(lines.join('\n'))) } catch (e) { /* ignore malformed frame */ }
  }
  const fail = async (res) => {
    let msg = `请求失败 (${res.status})`
    try { const j = await res.json(); if (j.detail) msg = j.detail } catch (e) {}
    throw new Error(msg)
  }
  if (typeof fetch === 'function' && typeof TextDecoder === 'function') {
    const res = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body), signal })
    if (!res.ok) await fail(res)
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let idx
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        parse(buf.slice(0, idx))
        buf = buf.slice(idx + 2)
      }
    }
    if (buf.trim()) parse(buf)
    return
  }
  const text = await new Promise((resolve, reject) => {
    uni.request({
      url, method: 'POST', data: body, header: headers, timeout: 300000,
      success: (r) => (r.statusCode >= 200 && r.statusCode < 300
        ? resolve(typeof r.data === 'string' ? r.data : JSON.stringify(r.data))
        : reject(new Error((r.data && r.data.detail) || `请求失败 (${r.statusCode})`))),
      fail: (e) => reject(new Error(e.errMsg || '网络错误')),
    })
  })
  for (const block of text.split('\n\n')) parse(block)
}
```

- [ ] **Step 2: 构建验证**

Run: `cd client && npm run build:h5`
Expected: 构建成功（无语法错误）

---

### Task 6: 方案 tab 改为「对话 / 方案」列表页

**Files:**
- Modify: `client/src/pages/plan/plan.vue`（整体重写，删除表单卡片）
- Modify: `client/src/pages.json`（注册 `pages/plan/chat`，标题「起号专家」）

**Interfaces:**
- Consumes: `api.get`、`goLogin`、`isLoggedIn`、`fmtTime`；`GET /api/plan-sessions`、`GET /api/plans`。
- Produces: 页面跳转 `/pages/plan/chat?id={session_id}` 与 `/pages/plan/chat`（新建）。

- [ ] **Step 1: 重写 `plan.vue`**

结构：`tab` 状态 `'chat' | 'plan'`；对话列表项显示标题、`fmtTime(updated_at)`、`plan_id` 时显示「已生成方案」；「+ 新建对话」按钮 `uni.navigateTo({url:'/pages/plan/chat'})`；方案 tab 沿用原来的方案列表渲染与跳转。访客（未登录）两个 tab 都显示「登录后查看」并在点击时 `goLogin()`。`onShow` 并行拉两个列表（未登录不拉）。删除原表单相关的 `form / accounts / personas / submit / submitting` 代码与模板。

`pages.json` 的 `pages` 数组在 `pages/plan/detail` 之后加：

```json
{ "path": "pages/plan/chat", "style": { "navigationBarTitleText": "起号专家" } }
```

- [ ] **Step 2: 构建验证**

Run: `cd client && npm run build:h5`
Expected: 构建成功

---

### Task 7: 聊天页 `chat.vue`

**Files:**
- Create: `client/src/pages/plan/chat.vue`

**Interfaces:**
- Consumes: `streamChat`（Task 5）、`api.get`、`api.post`、`mdToHtml`、`requireLogin`；`GET /api/plan-sessions/{id}`、`GET /api/plans/{plan_id}`、`POST /api/plan-sessions/{id}/plan`、`GET /api/agent/status`、`GET /api/accounts`、`POST /api/plans`（降级）。
- Produces: 可点击的方案卡片跳转 `/pages/plan/detail?id={plan_id}`。

- [ ] **Step 1: 实现页面**

要点（缺一不可）：
- `onLoad(query)`：`query.id` 存在则 `GET /api/plan-sessions/{id}` 填充 `session` 与 `messages`；否则 `newSession = true`，显示静态开场白（不调 LLM），拉 `/api/accounts` 提供「结合账号」picker（默认「全新账号」）。
- `status`：`GET /api/agent/status`，`llm_configured === false` 时禁用输入并显示「AI 未启用」提示与「模板快速生成」表单（赛道、目标 → `POST /api/plans` → `uni.navigateTo('/pages/plan/detail?id=')`）。
- 消息模型：`{ id, role, content, plan_id, planState, streaming }`；渲染时 `messages` 中 `plan_id` 且 `role === 'assistant'` 的项按方案卡片显示（生成中 / 已完成·查看方案 / 失败·查看错误），点击跳 detail。
- 发送 `send()`：
  1. 空消息或 `sending` 时返回；
  2. push 用户消息 + 一个 `streaming: true` 的空 assistant 气泡，滚到底；
  3. `AbortController` 存入 `controller` 变量；
  4. 调 `streamChat({ session_id: session?.id, account_id: newSession ? pickedAccountId : undefined, message }, { signal, onSession: (d) => { session = { id: d.session_id, title: d.title }; newSession = false; 回填用户消息 id }, onDelta: (t) => { 累加到 streaming 气泡（50ms 节流） }, onDone: (d) => { 气泡内容 = d.message.content，id = d.message.id，streaming = false }, onError: (e) => { 气泡内容为空则移除气泡，toast 错误，输入恢复 } })`；
  5. finally 清 `sending`。
- 「生成起号方案」按钮：`session` 为空或 `sending` 时禁用；点击 `POST /api/plan-sessions/{id}/plan` → push 方案卡片消息（`plan_id`，`planState: 'pending'`）→ `pollPlan(plan_id)` 每 3s `GET /api/plans/{plan_id}` 更新 `planState`（`done`/`failed` 停止）。
- 页面加载时对已存在的 `plan_id` 消息补一次 `GET /api/plans/{id}` 填充状态。
- `onUnload`：`controller && controller.abort()`；清所有定时器。
- 样式复用 `App.vue` 的 `.card/.input/.btn/.tag`；页面高度 `calc(100vh - var(--window-top) - var(--window-bottom))`；user 气泡靠右红底白字，assistant 靠左白底，`rich-text :nodes="mdToHtml(...)"`。

- [ ] **Step 2: 构建验证**

Run: `cd client && npm run build:h5`
Expected: 构建成功

---

### Task 8: 端到端冒烟

**Files:**
- 无新增文件（验证任务）

**Interfaces:**
- Consumes: 全部前置任务。

- [ ] **Step 1: 后端全量测试**

Run: `cd backend && uv run pytest -q`
Expected: 全部 PASS

- [ ] **Step 2: 启动并验证流式**

1. 重启后端：`cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000`。
2. `curl -N -X POST http://127.0.0.1:8000/api/plan-sessions/chat -H "Authorization: Bearer <token>" -H 'Content-Type: application/json' -d '{"message":"我想做健身，30天涨粉1000"}'`。
3. 期望：先收到 `event: session`，随后多个 `event: delta`（逐字），最后 `event: done`；`GET /api/plan-sessions` 能看到该会话。
   - 若 `LLM_API_KEY` 为空，先确认返回 503，再说明聊天需配置模型（模板快速生成仍可用）。

- [ ] **Step 3: 前端冒烟**

1. `cd client && npm run dev:h5`，浏览器打开 `http://localhost:5173`。
2. 登录 → 方案 tab → 新建对话 → 发送消息，确认文字逐字出现、返回列表能看到会话、重新进入可回看、点「生成起号方案」出现卡片并能打开详情。
3. 记录结果；构建产物 `npm run build:h5` 通过。

---

## Self-Review

- **Spec coverage**：数据模型（T1）、会话列表/详情/删除（T2）、SSE 聊天含新建/续聊/越权/未配置（T3）、方案生成含模板/meta/前置校验（T4）、前端流式封装与降级（T5）、双 tab 列表（T6）、聊天页与方案卡片（T7）、端到端（T8）。LLM 未配置降级在前端 T7 与后端 T3/T4 均覆盖。
- **Type consistency**：`chat_in_session_stream(db, user_id, persona_key, account, history, message)`、`extract_plan_meta(db, user_id, persona_key, dialog, fallback_niche)`、`generate_plan_from_session(plan_id, session_id)`、`run_agent_stream(persona, skills, prompt, user_id)` 在接口块与实现步骤中一致。
- **Review Focus 对照**：1 → T3 `error` 帧路径；2 → T3 的 `SessionLocal` 实现步骤；3 → T2/T3 越权测试；4 → T4 两个 400 测试；5 → T7 `onUnload`/`AbortController` 与 T5 降级分支。
