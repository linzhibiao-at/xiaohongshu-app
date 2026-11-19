import json
from datetime import datetime

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import GrowthPlan, PlanMessage, PlanSession
from app.services import agent_tasks


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


async def _seed_session(user_id: int, title: str = "健身起号", with_plan: bool = False):
    async with SessionLocal() as db:
        session = PlanSession(user_id=user_id, title=title, updated_at=datetime.now())
        db.add(session)
        await db.commit()
        await db.refresh(session)
        db.add(PlanMessage(session_id=session.id, role="user", content="我想做健身"))
        plan_id = None
        if with_plan:
            plan = GrowthPlan(user_id=user_id, niche=title, goal="涨粉", content="x", status="done")
            db.add(plan)
            await db.commit()
            await db.refresh(plan)
            db.add(PlanMessage(session_id=session.id, role="assistant", content="", plan_id=plan.id))
            plan_id = plan.id
        await db.commit()
        return session.id, plan_id


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
    assert (await client.get(f"/api/plan-sessions/{sid}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/plans/{plan_id}", headers=headers)).status_code == 200


async def test_session_foreign_access_404(client, create_user):
    _, other_headers = await create_user("chat_other")
    owner_id, owner_headers = await create_user("chat_owner2")
    sid, _ = await _seed_session(owner_id)
    assert (await client.get(f"/api/plan-sessions/{sid}", headers=other_headers)).status_code == 404
    assert (await client.get("/api/plan-sessions", headers=other_headers)).json() == []
    assert (await client.delete(f"/api/plan-sessions/{sid}", headers=other_headers)).status_code == 404
    assert (await client.get(f"/api/plan-sessions/{sid}", headers=owner_headers)).status_code == 200


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


async def test_chat_stream_foreign_session_404(client, create_user, fake_stream):
    _, other = await create_user("chat_stream_other")
    owner_id, _ = await create_user("chat_stream_owner")
    sid, _ = await _seed_session(owner_id)
    async with client.stream(
        "POST", "/api/plan-sessions/chat", headers=other, json={"session_id": sid, "message": "hi"}
    ) as res:
        assert res.status_code == 404


async def test_chat_stream_requires_llm(client, create_user):
    # conftest 已指定 LLM_API_KEY=""，无需 mock
    _, headers = await create_user("chat_no_llm")
    async with client.stream("POST", "/api/plan-sessions/chat", headers=headers, json={"message": "hi"}) as res:
        assert res.status_code == 503


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
