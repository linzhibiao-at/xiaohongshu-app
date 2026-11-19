# 微信登录（公众号网页授权）设计

日期：2026-11-19
范围：client（uni-app H5）+ backend（FastAPI）

## 1. 意图与为什么

**现状**：`client/src/pages/login/login.vue` 只有用户名 + 密码表单，后端仅 `POST /api/auth/login`
（`User.password_hash` bcrypt 校验后签发 HS256 JWT）。库里没有第三方身份表——此前的小红书扫码登录
只出了设计、未编码。

**需求**：用户在微信内置浏览器打开 H5 时，可以用微信身份一键登录，登录后与账密登录签发同一套 JWT，
各页面无感。首次登录自动建号，用户不必知道自己的"用户名"是什么。

**为什么是公众号网页授权**：用户已确认登录发生在微信内 H5 场景，服务号网页授权（`snsapi_userinfo`）
是唯一无需额外下载/扫码的路径，能在微信内置浏览器里静默完成跳转授权。

## 2. 目标 / 非目标

**目标**

1. 微信内 H5 点一次按钮完成登录；同一 openid 再次登录复用同一账号。
2. `appsecret`、`state`、`code`、一次性 `ticket` 的敏感环节全部留在服务端。
3. `WECHAT_MODE=mock` 时无凭证、无备案域名、无外网也能在本地跑通完整登录闭环。
4. 身份层（`UserIdentity` + ticket 换 JWT）可被后续小红书登录直接复用。

**非目标（本期不做）**

- 桌面浏览器扫码（开放平台网站应用）、App 端微信 SDK、小程序登录。
- 已登录态绑定 / 解绑微信、手机号绑定、历史账号自动合并。
- 微信昵称头像在 C 端页面展示（仅存库，`UserOut` 不变，管理后台零影响）。

**反目标（做了就算失败）**

- 前端产物里出现 `appsecret`，或日志里出现 `code` / `secret` / `ticket`。
- 微信用户按昵称/手机号自动并入同名老账号（冒领风险，沿用既有决策）。
- 改动 `users` 表已有列或影响账密登录、管理后台用户接口。

## 3. 约束

| 约束 | 影响 |
|---|---|
| 微信网页授权仅在微信内置浏览器生效，授权回调域名必须 ICP 备案 | 本地真机只能靠公众号测试号；正式联调需备案域名。因此 mock 是必要条件而非可选项 |
| 项目无 Alembic，表结构靠 `create_all` + `db._LATE_COLUMNS` 补列 | 本次只**新增表**，不给 `users` 加列，因此无需迁移 |
| client 是 uni-app H5，`h5.router.mode = hash` | 回跳地址形如 `http://host/#/pages/login/login?ticket=xxx`，`onLoad` 可直接取 `ticket` |
| 既有令牌体系 `security.create_token(user_id, role)`（HS256） | 微信登录只负责"拿到 user_id"，签发逻辑完全复用 |
| Redis 已是项目核心依赖（互动队列、分析缓存、技能版本） | state/ticket 用 Redis，测试用既有 fakeredis 夹具 |

## 4. 方案（已选：A）

### 4.1 采用方案：后端统一身份层 + 授权中转

前端只做两件事：跳到 `authorize`，以及回跳后拿 `ticket` 换 JWT。

```
login.vue 点「微信登录」
  → GET /api/auth/wechat/authorize
  → [live] 302 到 open.weixin.qq.com/connect/oauth2/authorize?appid=…&scope=snsapi_userinfo&state=<jwt>
  → [mock] 302 到 /api/auth/wechat/callback?code=mock_xxx&state=<jwt>
  → 微信授权后回 GET /api/auth/wechat/callback?code=&state=
  → 校验 state → provider 用 code 换 openid + 拉 userinfo
  → upsert User + UserIdentity → 写一次性 ticket（Redis，60s）
  → 302 到 {client_base_url}/#/pages/login/login?ticket=<ticket>
  → 前端 POST /api/auth/wechat/exchange {ticket} → {token, user}
```

**被否的两条**：(B) 前端解析 `code` 直接换 JWT——少一张表，但 `state` 校验要靠前端配合、未来扫码/App
通道又要另造入口；(C) `uni.login({provider:'weixin'})`——要求把公众号 secret 填进 `manifest.json`，
secret 会进前端产物，直接排除。

### 4.2 数据模型

新增 `app/models.py::UserIdentity`（只新增表，`create_all` 自动建）：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | int PK | |
| `user_id` | FK users.id, index | 绑定的平台用户 |
| `provider` | String(16), default `wechat` | 预留 `xhs` 等 |
| `provider_uid` | String(128) | 公众号 openid |
| `unionid` | String(128), default `""` | 绑定开放平台后才有，预留给多应用合并 |
| `nickname` | String(64), default `""` | 微信昵称（仅存库） |
| `avatar` | String(512), default `""` | 微信头像 URL |
| `created_at` | DateTime | 复用 `TimestampMixin` |

约束：`UniqueConstraint("provider", "provider_uid")`。

**首次登录建号**：`username = "wx_" + token_hex(4)`（唯一键冲突时重试，最多 5 次），
`password_hash = hash_password(token_urlsafe(24))`（随机占位，保持 NOT NULL 且用户无法用密码登录），
`role = "user"`。`User` 表本身不加列、`UserOut` 不变。

**登录匹配规则**：按 `(provider, provider_uid)` 查身份 → 有则复用其 `user`；无则建号并写身份。
不走任何"按昵称/手机号合并"，也即同一个人的老账密账号与微信账号默认是两套（后续可做显式绑定）。

### 4.3 配置（`app/config.py` + `.env.example`）

```python
wechat_mode: str = "mock"            # mock | live
wechat_app_id: str = ""
wechat_app_secret: str = ""
wechat_mock_openid: str = "mock_openid_dev"   # mock 模式固定 openid，重复登录复用同一账号
client_base_url: str = "http://localhost:5173"  # callback 回跳前端的 origin
```

`live` 但 `app_id`/`app_secret` 缺失时不报错，`/api/auth/methods` 直接返回 `enabled: false`，
前端隐藏按钮——避免部署期因配置缺失导致登录页崩掉。

### 4.4 后端结构

新增 `app/auth/wechat.py`（与 `app/xhs/base.py` 同风格：抽象 + 按配置选实现）：

```python
@dataclass
class WeChatUser:  # openid / unionid / nickname / avatar
    ...

class WeChatProvider(Protocol):
    async def exchange_code(self, code: str) -> WeChatUser: ...

class MockProvider:      # 不发网络请求，返回 settings.wechat_mock_openid 的假资料
class OfficialProvider:  # GET /sns/oauth2/access_token + GET /sns/userinfo
def build_provider() -> WeChatProvider | None: ...   # None = 未配置
```

`state` 用无状态签名 JWT（`{purpose: "wechat_state", nonce}`，exp 10 分钟），不落库；
`ticket` 落 Redis `wechat:ticket:<t>` → `user_id`，`ex=60`，exchange 时 `getdel` 保证一次性。

三个实现细节，避免实现时靠猜：

- `state` 的签名/校验单独写（同一个 `jwt_secret`，但**不复用** `security.decode_token`），并强制
  `purpose == "wechat_state"`：账密登录签发的 access token 没有 `sub` 以外的用途标记，被当作 state 传入时应验签失败。
- `MockProvider.exchange_code(code)` **忽略** `code` 内容，固定返回 `settings.wechat_mock_openid` 的假资料
  （昵称「微信测试用户」、头像空串），因此 mock 不需要任何网络请求。
- `getdel` 若在 fakeredis 上不可用，降级为 `get` + `delete`（测试层面无差异）；Redis 抛连接异常时
  exchange 统一 503，而不是 500。

### 4.5 接口契约（`app/api/user_api.py`，auth 段之后新增）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/auth/methods` | 返回 `{"password": true, "wechat": {"enabled": bool, "mode": "mock"\|"live"}}` |
| GET | `/api/auth/wechat/authorize` | 302：live 跳微信，mock 直接跳自家 callback；provider 未配置 → 404「微信登录未配置」 |
| GET | `/api/auth/wechat/callback` | 校验 state → 换 openid → upsert → 写 ticket → 302 回前端；state 失效/换 code 失败 → 302 回 `/#/pages/login/login?error=wechat_failed`（不让用户停在裸 JSON 页） |
| POST | `/api/auth/wechat/exchange` | body `{"ticket": str}` → `{"token", "user": UserOut}`；ticket 不存在/已用/过期 → 401「登录凭证已失效，请重新授权」；用户停用/删除 → 403 |

回调地址由 `request.base_url` 拼 `/api/auth/wechat/callback`，不在配置里再存一份——反代必须保留 `Host`，
正式环境的 `Host` 必须落在公众号配置的授权域名内。

### 4.6 前端

`login.vue` 布局：

```
┌────────────────────────────┐
│         小红书运营助手        │
│   起号方案 · 流量分析 · AI 助手 │
│                            │
│   [  微信登录  ]  ← #07c160 │
│   ───── 或使用账号密码 ─────  │
│   [用户名           ]        │
│   [密码             ]        │
│   [      登录        ]      │
└────────────────────────────┘
```

- `onLoad` 时拉 `/api/auth/methods`：`wechat.enabled` 为假则整块隐藏；`onLoad(options)` 里若有
  `ticket` 立即 `exchangeWechatTicket` → `setAuth` → `uni.switchTab('/pages/home/home')`（与账密登录一致）；
  若有 `error=wechat_failed` 则 toast「微信授权未完成，请重试」。
- 点击按钮：`location.href = BASE + '/api/auth/wechat/authorize'`（必须整页跳转，`uni.request` 不跟随 302）。
- `mode=live` 且 UA 不含 `MicroMessenger` → toast「请在微信中打开链接」且不发跳转；`mode=mock` 任何时候可点。
- `api.js` 增两个函数：`startWechatLogin()`（整页跳转）、`exchangeWechatTicket(ticket)`（POST exchange）。

### 4.7 安全要求

- `appsecret` 只出现在后端环境变量；`code` 拿到即用、不落库不落日志；`ticket` 只出现在 URL 与 Redis。
- `state` 必须验签 + 校验 `purpose`，防 CSRF 与串用。
- `ticket` 60 秒一次性；exchange 前校验用户 `is_active` 且未 `is_deleted`。
- 回跳前端只用 `settings.client_base_url` 拼接，不接受来自请求参数的外部 URL（避免开放重定向）。

## 5. 我的判断（可被推翻的取舍）

1. **ticket 用 Redis 而非新表**：零迁移、单行 `getdel`，与项目既有 Redis 用法一致；Redis 不可用时 exchange
   返回 503「微信登录暂不可用，请使用账号密码登录」，账密登录不受影响。
2. **mock 固定 openid**（`WECHAT_MOCK_OPENID`）：本地反复登录始终是同一个账号，便于联调；代价是 mock 下
   无法模拟"新用户"。需要时改环境变量即可。
3. **昵称头像只存 `UserIdentity`**：不改 `users` 表、不动 `UserOut`，管理后台与既有测试零回归。
4. **登录后固定回 `home`**：与现有账密登录行为一致，不做 `redirect` 参数（本期没有"回到原页面"的需求）。

## 6. 测试计划（`backend/tests/test_wechat_login.py`）

1. `/api/auth/methods`：mock 模式 → `wechat.enabled == true, mode == "mock"`；`live` 且缺 secret → `enabled == false`。
2. mock 全链路：`GET /api/auth/wechat/authorize` → 302 到 callback → 302 到 `client_base_url` 带 `ticket`
   → `POST exchange` 得 `token`，可访问 `/api/auth/me`。
3. ticket 一次性：同一 ticket 第二次 exchange → 401。
4. 同一 openid 二次登录复用同一 `user.id`，`users` 行数不增加。
5. `state` 被篡改 / `purpose` 不符 → callback 重定向到 `error=wechat_failed`，不建号。

## 7. 分期

- **P0（本次）**：表 + provider（mock）+ 四个接口 + 登录页改造 + 上述测试。
- **P1**：公众号凭证就绪后切 `WECHAT_MODE=live`；已登录态绑定/解绑接口（复用同表）。
- **P2**：桌面扫码（开放平台网站应用）复用 `UserIdentity` + ticket 换 JWT，只加一个入口。

## 8. 风险与回滚

- 入口可通过 `WECHAT_MODE` 关掉，接口与账密登录完全解耦，回滚只需前端隐藏按钮。
- 真实联调风险集中在微信侧配置（授权域名、测试号白名单），mock 模式已把这些风险隔离在发布前。
