import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from urllib.parse import parse_qs, urlsplit

from app.auth.wechat import MockProvider, OfficialProvider, build_provider
from app.auth.session import TicketStoreError, consume_ticket, issue_ticket, new_state, read_state
from app.config import get_settings
from app.models import User, UserIdentity
from app.security import create_token


@pytest.fixture
def wx_env(monkeypatch):
    """临时改配置并清掉 lru_cache；测试结束恢复环境并再次清缓存。"""

    def _set(**kw):
        for k, v in kw.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


def test_build_provider_mock_ignores_credentials():
    assert isinstance(build_provider(), MockProvider)


def test_build_provider_live_without_secret_is_none(wx_env):
    wx_env(WECHAT_MODE="live", WECHAT_APP_ID="", WECHAT_APP_SECRET="")
    assert build_provider() is None


def test_build_provider_live_with_credentials(wx_env):
    wx_env(WECHAT_MODE="live", WECHAT_APP_ID="wx123", WECHAT_APP_SECRET="s3cret")
    assert isinstance(build_provider(), OfficialProvider)


async def test_mock_exchange_returns_configured_openid(wx_env):
    wx_env(WECHAT_MOCK_OPENID="mock_openid_xyz", WECHAT_MOCK_NICKNAME="测试昵称")
    wx = await MockProvider().exchange_code("anything")
    assert wx.openid == "mock_openid_xyz"
    assert wx.nickname == "测试昵称"


async def test_identity_unique_per_provider(db):
    user = User(username="wx_unique_case", password_hash="x", role="user")
    db.add(user)
    await db.flush()
    db.add(UserIdentity(user_id=user.id, provider="wechat", provider_uid="dup-openid"))
    await db.commit()
    db.add(UserIdentity(user_id=user.id, provider="wechat", provider_uid="dup-openid"))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


def test_state_roundtrip_and_rejects_tampering():
    state = new_state()
    assert read_state(state) is True
    assert read_state(state + "x") is False
    assert read_state("") is False


def test_access_token_is_not_a_valid_state():
    assert read_state(create_token(1, "user")) is False


async def test_ticket_is_single_use():
    ticket = await issue_ticket(42)
    assert len(ticket) >= 16
    assert await consume_ticket(ticket) == 42
    assert await consume_ticket(ticket) is None


async def test_ticket_store_error_propagates(monkeypatch):
    import app.auth.session as mod

    class Broken:
        async def getdel(self, *_a, **_k):
            raise ConnectionError("redis down")

        async def get(self, *_a, **_k):
            raise ConnectionError("redis down")

        async def set(self, *_a, **_k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(mod, "get_redis", lambda: Broken())
    with pytest.raises(TicketStoreError):
        await consume_ticket("whatever")
    with pytest.raises(TicketStoreError):
        await issue_ticket(1)


MOCK_OPENID = "mock_openid_task3"


def _ticket_of(location: str) -> str:
    """回跳地址是 hash 路由：ticket 在 fragment 的 query 里。"""
    frag = urlsplit(location).fragment
    return parse_qs(frag.split("?", 1)[1])["ticket"][0]


async def _mock_login(client) -> str:
    r = await client.get("/api/auth/wechat/authorize", follow_redirects=False)
    assert r.status_code == 302
    callback = urlsplit(r.headers["location"])
    r2 = await client.get(f"{callback.path}?{callback.query}", follow_redirects=False)
    assert r2.status_code == 302
    return r2.headers["location"]


async def test_methods_reports_mock_mode(client, wx_env):
    wx_env(WECHAT_MODE="mock")
    r = await client.get("/api/auth/methods")
    assert r.json() == {"password": True, "wechat": {"enabled": True, "mode": "mock"}}


async def test_methods_hides_wechat_in_live_without_credentials(client, wx_env):
    wx_env(WECHAT_MODE="live", WECHAT_APP_ID="", WECHAT_APP_SECRET="")
    assert (await client.get("/api/auth/methods")).json()["wechat"]["enabled"] is False


async def test_authorize_redirects_to_own_callback_in_mock(client, wx_env):
    wx_env(WECHAT_MOCK_OPENID=MOCK_OPENID)
    r = await client.get("/api/auth/wechat/authorize", follow_redirects=False)
    assert r.status_code == 302
    assert urlsplit(r.headers["location"]).path == "/api/auth/wechat/callback"
    assert "state=" in r.headers["location"]


async def test_callback_redirects_with_ticket_on_client_base_url(client, wx_env):
    wx_env(WECHAT_MOCK_OPENID=MOCK_OPENID)
    location = await _mock_login(client)
    # 不硬编码 origin：backend/.env 可能已设定 CLIENT_BASE_URL
    assert location.startswith(get_settings().client_base_url + "/#/pages/login/login?ticket=")
    assert _ticket_of(location)


async def test_tampered_state_is_rejected_without_creating_user(client, db, wx_env):
    wx_env(WECHAT_MOCK_OPENID=MOCK_OPENID + "-bad")
    r = await client.get("/api/auth/wechat/callback?code=mock_code&state=bad", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].endswith("?error=wechat_failed")
    assert await db.scalar(
        select(UserIdentity).where(UserIdentity.provider_uid == MOCK_OPENID + "-bad")
    ) is None


async def test_same_openid_reuses_user(client, db, wx_env):
    wx_env(WECHAT_MOCK_OPENID=MOCK_OPENID + "-reuse")
    await _mock_login(client)
    await _mock_login(client)
    rows = (await db.scalars(
        select(UserIdentity).where(UserIdentity.provider_uid == MOCK_OPENID + "-reuse")
    )).all()
    assert len(rows) == 1


async def test_exchange_returns_usable_token(client, wx_env):
    wx_env(WECHAT_MOCK_OPENID=MOCK_OPENID + "-exchange")
    ticket = _ticket_of(await _mock_login(client))
    r = await client.post("/api/auth/wechat/exchange", json={"ticket": ticket})
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["username"].startswith("wx_")
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200 and me.json()["id"] == body["user"]["id"]


async def test_exchange_ticket_is_single_use(client, wx_env):
    wx_env(WECHAT_MOCK_OPENID=MOCK_OPENID + "-single")
    ticket = _ticket_of(await _mock_login(client))
    assert (await client.post("/api/auth/wechat/exchange", json={"ticket": ticket})).status_code == 200
    assert (await client.post("/api/auth/wechat/exchange", json={"ticket": ticket})).status_code == 401


async def test_exchange_unknown_ticket_is_401(client):
    assert (await client.post("/api/auth/wechat/exchange", json={"ticket": "nope-not-a-ticket"})).status_code == 401


async def test_disabled_user_cannot_exchange(client, db, wx_env):
    wx_env(WECHAT_MOCK_OPENID=MOCK_OPENID + "-disabled")
    user = User(username="wx_disabled_case", password_hash="x", role="user", is_active=False)
    db.add(user)
    await db.flush()
    db.add(UserIdentity(user_id=user.id, provider="wechat", provider_uid=MOCK_OPENID + "-disabled"))
    await db.commit()
    ticket = _ticket_of(await _mock_login(client))
    assert (await client.post("/api/auth/wechat/exchange", json={"ticket": ticket})).status_code == 403


async def test_redis_down_exchange_is_503(client, wx_env, monkeypatch):
    import app.auth.session as mod

    wx_env(WECHAT_MOCK_OPENID=MOCK_OPENID + "-redisfail")
    ticket = _ticket_of(await _mock_login(client))

    class Broken:
        async def getdel(self, *_a, **_k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(mod, "get_redis", lambda: Broken())
    r = await client.post("/api/auth/wechat/exchange", json={"ticket": ticket})
    assert r.status_code == 503


async def test_provider_network_error_redirects_to_login_error(client, wx_env, monkeypatch):
    import httpx

    import app.api.user_api as ua

    wx_env(WECHAT_MOCK_OPENID=MOCK_OPENID + "-neterr")

    class BrokenProvider:
        async def exchange_code(self, code):
            raise httpx.ConnectError("wechat unreachable")

    monkeypatch.setattr(ua, "build_provider", lambda: BrokenProvider())
    r = await client.get("/api/auth/wechat/authorize", follow_redirects=False)
    callback = urlsplit(r.headers["location"])
    r2 = await client.get(f"{callback.path}?{callback.query}", follow_redirects=False)
    assert r2.status_code == 302
    assert r2.headers["location"].endswith("?error=wechat_failed")
