from app.agents.plugins import load_builtin_personas, load_builtin_skills
from app.agents.runtime import build_system_prompt
from app.worker import run_once


async def _account(client, auth, **extra):
    r = await client.post("/api/accounts", headers=auth,
                          json={"xhs_id": "test_account", "nickname": "测试号", **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_auth_required_and_login(client, auth, create_user):
    assert (await client.get("/api/accounts")).status_code == 401
    assert (await client.get("/api/auth/me", headers=auth)).json()["role"] == "user"
    r = await client.post("/api/auth/login", json={"username": "admin", "password": "wrong-pass"})
    assert r.status_code == 401
    # plain users may use the C-end but never sign in to the admin console
    await create_user("scope_check")
    assert (await client.post("/api/auth/login", json={"username": "scope_check", "password": "secret123",
                                                       "scope": "admin"})).status_code == 403
    assert (await client.post("/api/auth/login", json={"username": "scope_check", "password": "secret123",
                                                       "scope": "client"})).status_code == 200


async def test_accounts_are_isolated_between_users(client, auth, create_user):
    account_id = await _account(client, auth)
    _, other_h = await create_user("intruder")
    assert (await client.get(f"/api/accounts/{account_id}/analytics", headers=other_h)).status_code == 404
    assert (await client.get("/api/accounts", headers=other_h)).json() == []


async def test_plan_falls_back_to_template_without_llm(client, auth):
    r = await client.post("/api/plans", headers=auth, json={"niche": "健身", "goal": "30天涨粉1000"})
    plan = (await client.get(f"/api/plans/{r.json()['id']}", headers=auth)).json()
    assert plan["status"] == "done"
    assert "健身" in plan["content"] and "未配置大模型" in plan["content"]


async def test_sync_and_analytics(client, auth):
    account_id = await _account(client, auth)
    assert (await client.post(f"/api/accounts/{account_id}/sync", headers=auth)).json()["synced"] == 12
    report = (await client.get(f"/api/accounts/{account_id}/analytics", headers=auth)).json()
    assert report["note_count"] == 12
    assert report["totals"]["views"] == sum(n["views"] for n in report["notes"])
    assert 0 < report["ctr"] < 1 and 0 < report["engagement_rate"] < 1
    assert report["notes"][0]["views"] >= report["notes"][-1]["views"]
    assert len(report["trend"]) == 1 and report["by_tag"] and report["by_publish_hour"]
    # a second sync adds snapshots, not notes
    await client.post(f"/api/accounts/{account_id}/sync", headers=auth)
    again = (await client.get(f"/api/accounts/{account_id}/analytics", headers=auth)).json()
    assert again["note_count"] == 12 and again["totals"] == report["totals"]


async def test_interaction_flow_approval_limits_and_failure(client, auth):
    account_id = await _account(client, auth)
    body = {"account_id": account_id, "target_note": "note-1", "actions": ["like", "collect", "comment"],
            "comment_text": "第三步的搭配思路很实用"}
    tasks = {t["action"]: t for t in (await client.post("/api/interactions", headers=auth, json=body)).json()}
    assert tasks["like"]["status"] == "queued" and tasks["collect"]["status"] == "queued"
    assert tasks["comment"]["status"] == "pending_approval"  # comments wait for the user

    assert await run_once() == 2
    statuses = {t["id"]: t["status"] for t in (await client.get("/api/interactions", headers=auth)).json()}
    assert statuses[tasks["like"]["id"]] == "done" and statuses[tasks["comment"]["id"]] == "pending_approval"

    r = await client.post(f"/api/interactions/{tasks['comment']['id']}/approve", headers=auth,
                          json={"comment_text": "改过的评论"})
    assert r.json()["status"] == "queued" and r.json()["comment_text"] == "改过的评论"
    await run_once()
    quota = (await client.get(f"/api/accounts/{account_id}/interaction-quota", headers=auth)).json()
    assert quota == {"used_today": 3, "daily_limit": 3, "min_interval_seconds": 0}

    # daily limit (3 in tests) reached -> next task fails with a clear reason
    over = (await client.post("/api/interactions", headers=auth,
                              json={"account_id": account_id, "target_note": "note-2", "actions": ["like"]})).json()[0]
    await run_once()
    listed = {t["id"]: t for t in (await client.get("/api/interactions", headers=auth)).json()}
    assert listed[over["id"]]["status"] == "failed" and "上限" in listed[over["id"]]["error"]


async def test_connector_failure_is_recorded_and_cancel_works(client, auth):
    account_id = await _account(client, auth)
    bad = (await client.post("/api/interactions", headers=auth,
                             json={"account_id": account_id, "target_note": "fail-note", "actions": ["like"]})).json()[0]
    await run_once()
    listed = {t["id"]: t for t in (await client.get("/api/interactions", headers=auth)).json()}
    assert listed[bad["id"]]["status"] == "failed" and "模拟失败" in listed[bad["id"]]["error"]

    queued = (await client.post("/api/interactions", headers=auth,
                                json={"account_id": account_id, "target_note": "n3", "actions": ["collect"]})).json()[0]
    assert (await client.post(f"/api/interactions/{queued['id']}/cancel", headers=auth)).json()["status"] == "cancelled"
    await run_once()  # cancelled task is skipped, not executed
    listed = {t["id"]: t for t in (await client.get("/api/interactions", headers=auth)).json()}
    assert listed[queued["id"]]["status"] == "cancelled"


async def test_comment_without_text_needs_llm(client, auth):
    account_id = await _account(client, auth)
    r = await client.post("/api/interactions", headers=auth, json={
        "account_id": account_id, "target_note": "n", "actions": ["comment"], "note_context": "一篇穿搭笔记"})
    assert r.status_code == 503


async def test_custom_persona_and_plugin_toggle(client, auth, admin):
    keys = {p["key"] for p in (await client.get("/api/personas", headers=auth)).json()}
    assert {"growth_coach", "data_analyst", "community_manager"} <= keys

    bad = await client.post("/api/personas", headers=auth, json={
        "name": "x", "system_prompt": "你是一个测试人格，回答要简短。", "skills": ["nope"]})
    assert bad.status_code == 400
    r = await client.post("/api/personas", headers=auth, json={
        "name": "毒舌点评", "system_prompt": "你是一个测试人格，回答要简短。", "skills": ["comment_writer"]})
    key = r.json()["key"]
    mine = [p for p in (await client.get("/api/personas", headers=auth)).json() if p["key"] == key]
    assert mine and mine[0]["mine"] and not mine[0]["builtin"]

    # normal users cannot reach the admin API
    assert (await client.get("/api/admin/plugins", headers=auth)).status_code == 403
    toggle = {"kind": "persona", "name": "data_analyst", "enabled": False}
    assert (await client.post("/api/admin/plugins/toggle", headers=admin, json=toggle)).status_code == 200
    keys = {p["key"] for p in (await client.get("/api/personas", headers=auth)).json()}
    assert "data_analyst" not in keys
    await client.post("/api/admin/plugins/toggle", headers=admin, json={**toggle, "enabled": True})

    await client.post("/api/admin/plugins/toggle", headers=admin,
                      json={"kind": "skill", "name": "data_sandbox", "enabled": False})
    names = {s["name"] for s in (await client.get("/api/skills", headers=auth)).json()}
    assert "data_sandbox" not in names and "traffic_analysis" in names
    await client.post("/api/admin/plugins/toggle", headers=admin,
                      json={"kind": "skill", "name": "data_sandbox", "enabled": True})


async def test_admin_console(client, auth, admin, create_user):
    stats = (await client.get("/api/admin/stats", headers=admin)).json()
    assert stats["users"] >= 2
    system = (await client.get("/api/admin/system", headers=admin)).json()
    assert system["health"] == {"mysql": "ok", "redis": "ok"} and system["available_connectors"] == ["mock"]

    # accounts are provisioned from the console, never self-registered
    created = await client.post("/api/admin/users", headers=admin,
                                json={"username": "provisioned", "password": "secret123"})
    assert created.status_code == 200 and created.json()["role"] == "user"
    dup = await client.post("/api/admin/users", headers=admin,
                            json={"username": "provisioned", "password": "secret123"})
    assert dup.status_code == 409
    assert (await client.post("/api/admin/users", headers=auth,
                              json={"username": "nope", "password": "secret123"})).status_code == 403

    # paginated list: 10 per page by default, only 10/20/50 allowed
    page1 = (await client.get("/api/admin/users", headers=admin)).json()
    assert page1["page"] == 1 and page1["page_size"] == 10 and page1["total"] >= 3
    assert len(page1["items"]) <= 10
    assert (await client.get("/api/admin/users", headers=admin, params={"page_size": 7})).status_code == 400

    # reset password: omit the password -> random 6 digits, returned once
    pwd_id, _ = await create_user("pwd_reset")
    r = await client.post(f"/api/admin/users/{pwd_id}/reset-password", headers=admin)
    assert r.status_code == 200 and r.json()["generated"] is True
    new_password = r.json()["password"]
    assert len(new_password) == 6 and new_password.isdigit()
    assert (await client.post("/api/auth/login", json={"username": "pwd_reset", "password": "secret123"})).status_code == 401
    assert (await client.post("/api/auth/login", json={"username": "pwd_reset", "password": new_password})).status_code == 200
    reset_logs = [log for log in (await client.get("/api/admin/audit-logs", headers=admin)).json()
                  if log["action"] == "user.reset_password" and "pwd_reset" in log["detail"]]
    assert reset_logs and new_password not in reset_logs[0]["detail"]

    # an explicitly entered password is honoured
    r = await client.post(f"/api/admin/users/{pwd_id}/reset-password", headers=admin,
                          json={"password": "chosen-pass"})
    assert r.json() == {"password": "chosen-pass", "generated": False}
    assert (await client.post("/api/auth/login", json={"username": "pwd_reset", "password": "chosen-pass"})).status_code == 200
    assert (await client.post(f"/api/admin/users/{pwd_id}/reset-password", headers=admin,
                              json={"password": "123"})).status_code == 422

    # soft delete: the login account disappears, every related row stays
    before = (await client.get("/api/admin/stats", headers=admin)).json()
    del_id, del_h = await create_user("to_delete")
    account_id = await _account(client, del_h)
    await client.post(f"/api/accounts/{account_id}/sync", headers=del_h)
    bound = (await client.get(f"/api/admin/users/{del_id}/accounts", headers=admin)).json()
    assert [a["id"] for a in bound] == [account_id] and bound[0]["xhs_id"] == "test_account"
    assert (await client.get(f"/api/admin/users/{del_id}/accounts", headers=auth)).status_code == 403
    assert (await client.delete(f"/api/admin/users/{del_id}", headers=admin)).json()["deleted"] == 1
    assert (await client.get("/api/accounts", headers=del_h)).status_code == 401
    after = (await client.get("/api/admin/stats", headers=admin)).json()
    # the deleted user's bound account and 12 synced notes are still there
    assert after["notes"] == before["notes"] + 12
    assert after["xhs_accounts"] == before["xhs_accounts"] + 1
    assert (await client.get("/api/admin/users", headers=admin, params={"q": "to_delete"})).json()["total"] == 0
    assert (await client.get(f"/api/admin/users/{del_id}/accounts", headers=admin)).status_code == 404
    assert (await client.delete(f"/api/admin/users/{del_id}", headers=admin)).status_code == 404

    # batch delete, and admins cannot delete themselves
    batch = [await create_user("batch_a"), await create_user("batch_b")]
    r = await client.post("/api/admin/users/delete", headers=admin, json={"ids": [uid for uid, _ in batch]})
    assert r.json()["deleted"] == 2
    me = (await client.get("/api/auth/me", headers=admin)).json()
    assert (await client.delete(f"/api/admin/users/{me['id']}", headers=admin)).status_code == 400

    users = (await client.get("/api/admin/users", headers=admin)).json()["items"]
    target = next(u for u in users if u["role"] == "user")
    r = await client.patch(f"/api/admin/users/{target['id']}", headers=admin, json={"is_active": False})
    assert r.json()["is_active"] is False
    logs = (await client.get("/api/admin/audit-logs", headers=admin)).json()
    assert any(log["action"] == "user.update" for log in logs)


async def test_disabled_user_is_locked_out(client, admin, create_user):
    user_id, headers = await create_user("to_disable")
    await client.patch(f"/api/admin/users/{user_id}", headers=admin, json={"is_active": False})
    assert (await client.get("/api/accounts", headers=headers)).status_code == 401


async def test_public_note_link_analysis(client):
    url = "https://www.xiaohongshu.com/explore/abc123def456?xsec_token=xyz"
    r = await client.post("/api/notes/analyze", json={"url": url})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["note"]["id"] == "abc123def456"
    assert 0 <= data["score"] <= 100 and data["verdict"] and data["suggestions"]
    assert data["metrics"]["impressions"] >= data["metrics"]["views"]
    # 同一链接结果稳定，访客不登录也能用
    again = await client.post("/api/notes/analyze", json={"url": url})
    assert again.json()["metrics"] == data["metrics"]

    short = await client.post("/api/notes/analyze", json={"url": "https://xhslink.com/a/bcdef"})
    assert short.status_code == 400
    unknown = await client.post("/api/notes/analyze", json={"url": "https://example.com/hello"})
    assert unknown.status_code == 400


def test_plugins_load_and_prompt_includes_skills():
    personas, skills = load_builtin_personas(), load_builtin_skills()
    assert set(skills) == {
        "growth_playbook",
        "comment_writer",
        "traffic_analysis",
        "data_sandbox",
        "chinese_ai_humanizer",
        "promotion-poster-creator",
        "xiaohongshu-growing",
        "xiaohongshu-skills",
    }
    for p in personas.values():
        assert set(p.skills) <= set(skills), p.key
    assert [f.__name__ for f in skills["traffic_analysis"].load_tools()] == ["list_my_accounts", "get_traffic_report"]
    coach = personas["growth_coach"]
    prompt = build_system_prompt(coach, [skills[n] for n in coach.skills])
    assert "小红书起号专家" in prompt and "run_python" in prompt
