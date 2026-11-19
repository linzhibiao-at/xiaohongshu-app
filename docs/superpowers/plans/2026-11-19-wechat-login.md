# 微信登录（公众号网页授权）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 client 在微信内置浏览器里通过公众号网页授权一键登录，首次登录自动建号，签发与账密登录同一套 JWT。

**Architecture:** 后端新增 `UserIdentity` 身份表与 `app/auth/`（provider 抽象 + state/ticket 工具），提供 authorize → callback → exchange 三个跳转/换票接口；前端只负责整页跳去 authorize、拿回跳 URL 上的 `ticket` 换 JWT。`WECHAT_MODE=mock` 只替换 provider 实现，callback 之后的建号、发 ticket、发 JWT 全链路与真实模式共用同一份代码。

**Tech Stack:** FastAPI + SQLAlchemy(async) + Redis(redis.asyncio) + PyJWT/httpx；uni-app Vue3 H5（`uni.request` + 整页跳转）。

**Spec:** `docs/superpowers/specs/2026-11-19-wechat-login-design.md`

## Global Constraints

- 本目录**不是 git 仓库**（`git rev-parse` 失败），因此没有 commit 步骤；每个任务以「目标测试通过 + 全量回归通过」收尾。
- `users` 表已有列一律不改、`UserOut` 不变；本期只新增表，靠现有 `create_all` 建表，不写迁移。
- `appsecret` 不得出现在前端代码/产物；`code`、`ticket`、`secret` 不得写日志。
- `WECHAT_MODE=mock` 下不得发起任何外网请求；默认值就是 `mock`。
- 微信授权回调只在微信内置浏览器生效，回调域名必须 ICP 备案；前端回跳地址由后端配置 `CLIENT_BASE_URL` 拼出，不接受请求参数里的外部 URL。
- client 是 hash 路由：回跳地址形如 `http://host/#/pages/login/login?ticket=xxx`，`ticket` 在 **fragment** 里，不在 search 里。
- 测试命令统一：`cd backend && uv run pytest`；前端构建：`cd client && npm run build:h5`。
- 新增设置项必须同时写进 `backend/.env.example`。

## Review Focus

1. **state 被篡改/过期/拿 access token 冒充**：合理预期是重定向回登录页并提示失败，且不建号。
2. **ticket 二次使用**：合理预期是 401「登录凭证已失效」，不能换出第二个 JWT。
3. **同一微信 openid 反复登录**：合理预期是复用同一账号，`users` 不增长。
4. **账号被后台停用/删除后用微信再次登录**：合理预期是 403，不得静默新建账号。
5. **Redis 不可用时 exchange**：合理预期是 503 且提示改用账号密码，而不是 500，账密登录不受影响。

---

### Task 1: 配置项、`UserIdentity` 表、微信 provider 抽象

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/.env.example`
- Modify: `backend/app/models.py`
- Create: `backend/app/auth/__init__.py`（空文件）
- Create: `backend/app/auth/wechat.py`
- Test: `backend/tests/test_wechat_login.py`（新建）

**Interfaces:**
- Produces:
  - `Settings.wechat_mode: str = "mock"`、`wechat_app_id: str = ""`、`wechat_app_secret: str = ""`、`wechat_mock_openid: str = "mock_openid_dev"`、`client_base_url: str = "http://localhost:5173"`
  - `models.UserIdentity`（`user_id, provider="wechat", provider_uid, unionid, nickname, avatar`）
  - `app.auth.wechat`：`WeChatError`、`WeChatUser(openid, unionid, nickname, avatar)`、`WeChatProvider(ABC).exchange_code(code: str) -> WeChatUser`、`MockProvider`、`OfficialProvider(app_id, app_secret)`、`build_provider() -> WeChatProvider | None`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_wechat_login.py
import pytest
from sqlalchemy.exc import IntegrityError

from app.auth.wechat import MockProvider, OfficialProvider, build_provider
from app.config import get_settings
from app.models import User, UserIdentity


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_wechat_login.py -v`
Expected: FAIL（`ModuleNotFoundError: app.auth.wechat`）

- [ ] **Step 3: 加配置项**

`app/config.py` 的 `Settings` 里，`xhs_connector` 之后追加：

```python
    # ---- 微信登录（公众号网页授权）----
    wechat_mode: str = "mock"  # mock | live
    wechat_app_id: str = ""
    wechat_app_secret: str = ""
    wechat_mock_openid: str = "mock_openid_dev"
    wechat_mock_nickname: str = "微信测试用户"
    client_base_url: str = "http://localhost:5173"
```

`.env.example` 追加同名段落（`WECHAT_MODE=mock`、`WECHAT_APP_ID=`、`WECHAT_APP_SECRET=`、`WECHAT_MOCK_OPENID=mock_openid_dev`、`WECHAT_MOCK_NICKNAME=微信测试用户`、`CLIENT_BASE_URL=http://localhost:5173`），并在注释里写明「live 需要 ICP 备案的授权回调域名」。

- [ ] **Step 4: 加 `UserIdentity` 模型**

`app/models.py` 里 `User` 之后插入（沿用文件既有的 `UniqueConstraint` 与 `TimestampMixin` 写法）：

```python
class UserIdentity(Base, TimestampMixin):
    """第三方身份（provider + 平台内唯一 id）到平台用户的绑定。"""

    __tablename__ = "user_identities"
    __table_args__ = (UniqueConstraint("provider", "provider_uid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16), default="wechat")  # wechat | xhs | ...
    provider_uid: Mapped[str] = mapped_column(String(128))  # 公众号 openid
    unionid: Mapped[str] = mapped_column(String(128), default="")
    nickname: Mapped[str] = mapped_column(String(64), default="")
    avatar: Mapped[str] = mapped_column(String(512), default="")
```

- [ ] **Step 5: 实现 provider**

`app/auth/wechat.py`：

```python
class WeChatError(Exception): ...


@dataclass
class WeChatUser:
    openid: str
    unionid: str = ""
    nickname: str = ""
    avatar: str = ""


class WeChatProvider(ABC):
    @abstractmethod
    async def exchange_code(self, code: str) -> WeChatUser: ...
```

- `MockProvider.exchange_code` 忽略 `code`，直接返回 `WeChatUser(openid=settings.wechat_mock_openid, nickname=settings.wechat_mock_nickname)`。
- `OfficialProvider(app_id, app_secret)` 用 `httpx.AsyncClient`（timeout 10s）调
  `https://api.weixin.qq.com/sns/oauth2/access_token?appid=&secret=&code=&grant_type=authorization_code`，
  取 `openid/access_token/unionid`；再调 `https://api.weixin.qq.com/sns/userinfo?access_token=&openid=&lang=zh_CN` 取 `nickname/headimgurl`。
  微信返回体带 `errcode` 时抛 `WeChatError(f"微信登录失败({errcode})")`，**不要把 code/secret 拼进异常消息**。
- `build_provider()`：`mode == "mock"` → `MockProvider()`；`mode == "live"` 且 `app_id` 与 `app_secret` 都非空 → `OfficialProvider(...)`；其余返回 `None`。

- [ ] **Step 6: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_wechat_login.py -v`
Expected: 5 passed

- [ ] **Step 7: 全量回归**

Run: `cd backend && uv run pytest`
Expected: 全部通过（既有用例不受影响；`init_db` 会自动建 `user_identities`）

---

### Task 2: state 签名与一次性 ticket

**Files:**
- Create: `backend/app/auth/session.py`
- Test: `backend/tests/test_wechat_login.py`（追加）

**Interfaces:**
- Produces:
  - `new_state() -> str`
  - `read_state(state: str) -> bool`
  - `issue_ticket(user_id: int) -> str`（async）
  - `consume_ticket(ticket: str) -> int | None`（async，Redis 异常抛 `TicketStoreError`）
  - `TicketStoreError`
- Consumes: `app.security.create_token` 的同款 `jwt_secret`；`app.redis_client.get_redis`。

- [ ] **Step 1: 写失败测试**

```python
from app.auth.session import TicketStoreError, consume_ticket, issue_ticket, new_state, read_state
from app.security import create_token


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_wechat_login.py -v -k "state or ticket"`
Expected: FAIL（`ModuleNotFoundError: app.auth.session`）

- [ ] **Step 3: 实现 `app/auth/session.py`**

- `new_state()`：`jwt.encode({"purpose": "wechat_state", "nonce": secrets.token_urlsafe(12), "exp": now + 10min}, jwt_secret, "HS256")`。
- `read_state(s)`：`jwt.decode(s, jwt_secret, ["HS256"])` 后校验 `payload.get("purpose") == "wechat_state"`，任何异常返回 `False`；**不复用 `security.decode_token`**（避免 access token 被当 state 用）。
- `issue_ticket(user_id)`：`t = secrets.token_urlsafe(24)`，`await get_redis().set(f"wechat:ticket:{t}", str(user_id), ex=60)`，Redis 异常转 `TicketStoreError`。
- `consume_ticket(t)`：优先 `getdel`，`AttributeError`（fakeredis 无此命令）时降级 `get` + `delete`；取不到返回 `None`，返回 `int(value)`；Redis 异常转 `TicketStoreError`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_wechat_login.py -v -k "state or ticket"`
Expected: 4 passed

---

### Task 3: `methods` / `authorize` / `callback` 与建号逻辑

**Files:**
- Modify: `backend/app/api/user_api.py`（`auth` 段之后、`xhs accounts` 段之前）
- Test: `backend/tests/test_wechat_login.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `build_provider` / `WeChatUser` / `WeChatError`，Task 2 的 `new_state` / `read_state` / `issue_ticket` / `TicketStoreError`。
- Produces（app 内部函数，Task 4 不再依赖）：
  - `async def _wechat_login(db: AsyncSession, wx: WeChatUser) -> User`
  - `GET /api/auth/methods`、`GET /api/auth/wechat/authorize`、`GET /api/auth/wechat/callback`

- [ ] **Step 1: 写失败测试**

```python
from urllib.parse import parse_qs, urlsplit

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
```

（测试文件顶部的 import 补 `from sqlalchemy import select` 与 `from app.models import User`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_wechat_login.py -v -k "methods or authorize or callback or reuse or tampered"`
Expected: FAIL（404，路由不存在）

- [ ] **Step 3: 实现三个读接口与建号函数**

`app/api/user_api.py` 新增 import：`Request`、`RedirectResponse`、`secrets`、`select`（已有）、
`from app.auth.session import TicketStoreError, issue_ticket, new_state, read_state`、
`from app.auth.wechat import WeChatError, WeChatUser, build_provider`、
`from app.models import UserIdentity`。

- `GET /api/auth/methods`：返回 `{"password": True, "wechat": {"enabled": build_provider() is not None, "mode": settings.wechat_mode}}`。
- `GET /api/auth/wechat/authorize`：`build_provider()` 为 `None` → `HTTPException(404, "微信登录未配置")`；否则
  `state = new_state()`，mock 模式 `302 {request.base_url}api/auth/wechat/callback?code=mock_code&state={state}`，
  live 模式 `302 https://open.weixin.qq.com/connect/oauth2/authorize?appid={quote(app_id)}&redirect_uri={quote(f"{request.base_url}api/auth/wechat/callback", safe="")}&response_type=code&scope=snsapi_userinfo&state={state}#wechat_redirect`。
  （`request.base_url` 自带结尾 `/`，反代需保留 `Host`。）
- `GET /api/auth/wechat/callback?code=&state=`：`read_state(state)` 为假、或 `code` 为空 → `302 {client_base_url}/#/pages/login/login?error=wechat_failed`；
  否则 `provider.exchange_code(code)` → `_wechat_login` → `issue_ticket(user.id)` → `302 ...?ticket={ticket}`。
  捕获 `(WeChatError, TicketStoreError)` 时同样重定向到 `error=wechat_failed`；其它异常不吞（保留 500 便于排障）。
- `_wechat_login(db, wx)`：按 `(provider="wechat", provider_uid=wx.openid)` 查 `UserIdentity`，命中则取 `User`
  （顺手用非空的 `wx.nickname/wx.avatar` 刷新身份记录后 commit）并返回；未命中则
  `username = "wx_" + secrets.token_hex(4)`（查重最多试 5 次）、
  `password_hash = hash_password(secrets.token_urlsafe(24))`、`role="user"` 建 `User`，
  `flush()` 后插 `UserIdentity(...)`，`commit()` 后返回。并发撞唯一键时捕获 `IntegrityError` → `rollback()` → 重新按身份查一次。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_wechat_login.py -v -k "methods or authorize or callback or reuse or tampered"`
Expected: 6 passed

---

### Task 4: `exchange` 换 JWT（含停用、单次消费、Redis 故障）

**Files:**
- Modify: `backend/app/api/user_api.py`
- Test: `backend/tests/test_wechat_login.py`（追加）

**Interfaces:**
- Consumes: Task 3 的路由与 `_wechat_login`；Task 2 的 `consume_ticket` / `TicketStoreError`。
- Produces: `POST /api/auth/wechat/exchange`，body `{"ticket": str}` → `{"token": str, "user": UserOut}`。

- [ ] **Step 1: 写失败测试**

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_wechat_login.py -v -k "exchange"`
Expected: FAIL（404）

- [ ] **Step 3: 实现 exchange**

```python
class WechatTicketIn(BaseModel):
    ticket: str = Field(min_length=8, max_length=128)


@router.post("/auth/wechat/exchange")
async def wechat_exchange(body: WechatTicketIn, db: AsyncSession = Depends(get_db)):
    ...
```

`consume_ticket` 抛 `TicketStoreError` → `HTTPException(503, "微信登录暂不可用，请使用账号密码登录")`；
返回 `None` → `HTTPException(401, "登录凭证已失效，请重新授权")`；
用户不存在 / `is_deleted` / `not is_active` → `HTTPException(403, "账号已被停用")`；
否则 `{"token": create_token(user.id, user.role), "user": UserOut.model_validate(user)}`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_wechat_login.py -v`
Expected: 全部 passed（Task 1~4 共约 18 项）

- [ ] **Step 5: 全量回归**

Run: `cd backend && uv run pytest`
Expected: 全部通过

---

### Task 5: 前端登录页与 API 封装

**Files:**
- Modify: `client/src/api.js`
- Modify: `client/src/pages/login/login.vue`

**Interfaces:**
- Consumes: 后端 `GET /api/auth/methods`、`GET /api/auth/wechat/authorize`、`POST /api/auth/wechat/exchange`。
- Produces（`api.js` 导出）：`getAuthMethods()`、`startWechatLogin()`、`exchangeWechatTicket(ticket)`。

- [ ] **Step 1: `api.js` 追加三个函数**

```js
export function getAuthMethods() {
  return api.get('/api/auth/methods', { silent: true })
}

// 授权必须在浏览器层整页跳转：uni.request 不会跟随 302。
// App 端没有 location 全局，用条件编译隔离，避免构建进 App 后运行时报错。
export function startWechatLogin() {
  // #ifdef H5
  location.href = `${BASE}/api/auth/wechat/authorize`
  // #endif
  // #ifndef H5
  uni.showToast({ title: '微信登录暂仅支持 H5', icon: 'none' })
  // #endif
}

export function exchangeWechatTicket(ticket) {
  return api.post('/api/auth/wechat/exchange', { ticket })
}
```

- [ ] **Step 2: 改造 `login.vue`**

`<script setup>` 增加：

- `import { onLoad } from '@dcloudio/uni-app'` 与 `exchangeWechatTicket / getAuthMethods / startWechatLogin`。
- `const wechat = ref({ enabled: false, mode: 'mock' })`、`const exchanging = ref(false)`。
- `loginWithWechat()`：`wechat.mode === 'live'` 且 UA 不含 `MicroMessenger` → `uni.showToast({ title: '请在微信中打开链接', icon: 'none' })` 并 return；否则 `startWechatLogin()`。
  UA 取值要兼容 App 端：`const ua = typeof navigator === 'undefined' ? '' : navigator.userAgent`。
- `onLoad(options = {})`：先 `getAuthMethods().then((m) => { wechat.value = m.wechat || wechat.value }).catch(() => {})`；
  `options.error === 'wechat_failed'` → toast「微信授权未完成，请重试」；
  `options.ticket` → `exchanging.value = true`，`exchangeWechatTicket(options.ticket)` 成功后 `setAuth(token, user)` + `uni.switchTab({ url: '/pages/home/home' })`，失败由 `api` 层 toast，`finally` 复位。

`<template>` 在账密表单之前插入（沿用 App.vue 的 `.btn` 体系）：

```vue
<button v-if="wechat.enabled" class="btn wechat-btn" :disabled="exchanging" @click="loginWithWechat">
  {{ exchanging ? '登录中…' : '微信登录' }}
</button>
<view v-if="wechat.enabled" class="muted" style="text-align: center; margin: 12px 0">或使用账号密码</view>
```

样式加在页面内的 `<style>`（scoped 或普通均可）：`.wechat-btn { background: #07c160; margin-bottom: 4px; }`。

- [ ] **Step 3: 构建验证**

Run: `cd client && npm run build:h5`
Expected: 构建成功、无语法错误（dist/ 产物中不出现 `appsecret`/`WECHAT_APP_SECRET`）

- [ ] **Step 4: 手工验证 checklist（本地 mock）**

1. `cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload`（MySQL/Redis 就绪）
2. `cd client && npm run dev:h5`，访问 `http://localhost:5173`
3. 登录页出现绿色「微信登录」按钮；点击后浏览器跳转、最终回到 `#/pages/login/login?ticket=…` 并自动进入「我的」页
4. 退出登录后重复步骤 3，账号不变（同一 `wx_` 用户名）

---

### Task 6: 端到端冒烟与文档收尾

**Files:**
- Modify: `backend/.env.example`（若 Task 1 已写全则仅复核）
- Verify: 全链路

- [ ] **Step 1: 后端接口冒烟（不依赖前端）**

```bash
cd backend && uv run pytest tests/test_wechat_login.py -v
```
Expected: 全部 passed

- [ ] **Step 2: 手工跑一次完整授权链（mock）**

浏览器打开 `http://localhost:5173/api/auth/wechat/authorize`（vite 代理转发），确认最终落在
`http://localhost:5173/#/pages/login/login?ticket=…`；把该 `ticket` 用 `curl -X POST .../api/auth/wechat/exchange`
换一次 token，再用该 token 请求 `/api/auth/me` 返回 200；重复 exchange 同一 ticket 返回 401。

- [ ] **Step 3: 回归确认**

Run: `cd backend && uv run pytest` 与 `cd client && npm run build:h5`
Expected: 后端全绿、前端构建成功；确认账密登录与管理后台用户接口行为未变。

- [ ] **Step 4: 收尾说明**

在最终汇报里列出：新增/修改文件清单、mock 与 live 的切换方法（`WECHAT_MODE` + 三个微信配置项 + `CLIENT_BASE_URL`）、以及上线前微信侧需要配置的授权域名。
