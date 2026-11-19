# 起号方案改为 AI 对话生成 · 设计文档

日期：2026-11-18
状态：待确认

## 1. 背景与意图

现在 client 的「方案」tab 是一张四字段表单（赛道 / 目标 / 结合账号 / 方案顾问），提交后后台一次性生成 6 段式方案。用户希望改成**用自然语言直接问小红书起号专家**：多轮对话、AI 追问关键信息，聊够了得到一套可执行的起号方案。

方案仍然要能回看、能复制，并保留管理台的方案统计——所以方案（`GrowthPlan`）继续落库，聊天记录单独落库，两者关联。

## 2. 目标 / 非目标

**目标**
- 「方案」tab 提供自然语言对话入口，人格固定为 `growth_coach`（小红书起号专家）。
- 对话持久化：可回看历史会话、接着聊、删除会话。
- 对话中随时可一键「生成起号方案」，基于整段对话生成正式方案并落 `GrowthPlan`。
- 聊天里出现「方案卡片」，点开进入现有方案详情页（复制、状态轮询不变）。
- 对话回复走 **SSE 流式**（`text/event-stream`），像打字机一样逐字出现；工具调用等待期间前端显示「思考中…」。
- LLM 未配置时不阻断：提示不可用，并保留模板快速生成。

**非目标**
- 一期不做方案正文的流式渲染：方案在后台任务里生成（要跑工具、可能 1–2 分钟），结果仍在详情页轮询展示。
- 不做语音输入、图片上传、多人共享会话。
- 不改 admin 方案管理页与 `POST /api/plans` 旧接口（现有测试与模板兜底依赖它）。
- 不把历史 `GrowthPlan` 回填成会话。

**反目标**
- 对话里的方案卡片消失、只剩一段 markdown 文本（丢回看与管理台统计）。
- 聊天记录只存 Redis（刷新即丢、无法回看）。

## 3. 现状（改动的落点）

- `client/src/pages/plan/plan.vue`：表单 + 方案列表 + 轮询；`detail.vue` 渲染方案 markdown。
- `POST /api/plans` → 建 `GrowthPlan` → `BackgroundTasks` 跑 `agent_tasks.generate_plan`（无 key 时用 `_template_plan` 兜底）。
- 已有聊天：`POST /api/agent/chat`，历史存 Redis（8 轮、24h），无会话 ID、刷新不可回看——本次复用它底层的 `run_agent` 能力，但会话历史改为 DB。
- `growth_coach.yaml` 的提示词本就要求"先确认赛道、目标人群、账号现状和每周可投入时间，再给方案"；`growth_playbook` 技能固定了方案 6 部分结构。
- `persona.skills` 含数据/沙盒类技能，绑定 `XhsAccount` 时 AI 可查账号数据（沿用 `generate_plan` 的账号上下文写法）。
- `TimestampMixin` 只有 `created_at`；MySQL 无 RETURNING，`server_default` 列必须应用侧给默认值（见 mixin 注释）。

## 4. 方案对比（已选 B）

- **A. 轻量聊天**：复用 `/api/agent/chat`，历史仍在 Redis。改动最小，但刷新丢、不可回看、聊天与方案无关联。否决。
- **B. 会话化对话 + 方案沉淀（采用）**：新增 `plan_sessions` / `plan_messages` 两张表；聊天落库；「生成方案」基于整段对话创建 `GrowthPlan`，聊天中插入方案卡片消息。复用现有方案详情、后台统计、模板兜底。代价是两张表 + 6 个接口 + 两个页面改造。
- **C. 纯聊天出方案**：不落库。最轻，但丢掉方案列表、回看、管理台统计。否决。

选择 B 的原因：用户要的是"聊出来的方案"，方案是交付物而对话是过程，两者都要留痕；且项目已有的 `GrowthPlan`、详情页、admin 统计正好承接交付物。

## 5. 设计

### 5.1 页面结构

```
tab「方案」(plan.vue)
├─ 顶部 segment：「对话」 | 「方案」
├─ 对话 tab
│   ├─ [+ 新建对话]
│   └─ 会话列表：标题 / 更新时间 / 「已生成方案」标签
│        └─ 点击 → chat.vue?id={session_id}
└─ 方案 tab
    └─ 原有方案列表（去掉顶部生成表单）
         └─ 点击 → detail.vue（不变）

chat.vue（新增二级页，pages.json 注册）
├─ 顶部：起号专家标签；新建态可选「结合账号」（picker，默认全新账号）
├─ 消息区（scroll-view，自动滚底）
│   ├─ 空态静态开场白（不调 LLM）
│   ├─ user 右红气泡 / assistant 左白气泡（mdToHtml 渲染）
│   └─ 方案卡片消息：生成中… / 已完成（查看方案）/ 失败（查看错误）
├─ 底部：「生成起号方案」按钮（常驻，无用户消息时禁用）+ 输入框 + 发送
└─ LLM 未配置：黄色提示条，输入禁用；显示「模板快速生成」（赛道/目标 + 生成 → 旧 POST /api/plans → detail）
```

新建对话进入时**不建空会话**：用户发第一条消息才 `POST /api/plan-sessions`（携带 message），避免列表出现空会话。会话创建后页面内持有 `session_id`，不跳转。

### 5.2 数据模型（新表，`create_all` 自动创建，无需 ALTER 旧表）

```python
class PlanSession(Base, TimestampMixin):
    __tablename__ = "plan_sessions"
    id: int PK
    user_id: int FK users.id, index
    account_id: int | None FK xhs_accounts.id, nullable
    title: str String(64), default ""          # 首条用户消息截 30 字
    persona_key: str String(64), default "growth_coach"
    updated_at: datetime default=datetime.now, onupdate=datetime.now

class PlanMessage(Base, TimestampMixin):
    __tablename__ = "plan_messages"
    id: int PK
    session_id: int FK plan_sessions.id, index
    role: str String(16)                       # user | assistant
    content: Text, default ""
    plan_id: int | None FK growth_plans.id, nullable   # 非空即方案卡片消息
```

`updated_at` 手写（mixin 没有），应用侧默认值，避免 MySQL server_default 的 MissingGreenlet 问题。

### 5.3 API 契约（全部 `current_user` + 归属校验）

```
POST   /api/plan-sessions/chat         {session_id?: int|null, account_id?: int|null, message: 1..4000}
       -> text/event-stream      # 无 session_id 即新建会话；一个端点覆盖首问与续聊
GET    /api/plan-sessions?limit=50     -> [PlanSessionOut]  # id,title,account_id,updated_at,plan_id
GET    /api/plan-sessions/{id}         -> {session, messages}
POST   /api/plan-sessions/{id}/plan    -> {plan_id}
DELETE /api/plan-sessions/{id}         -> {ok: true}        # 删会话与消息，GrowthPlan 保留
```

**SSE 帧序列**（`/chat` 端点）：

```
event: session   data: {"session_id": 12, "title": "...", "user_message": {...}}
event: delta     data: {"text": "先聊聊"}          # 多次，前端累加
event: done      data: {"message": {id, role:"assistant", content, created_at}}
event: error     data: {"detail": "AI 调用失败: ..."}   # 终止帧
```

- 响应头：`Cache-Control: no-cache`、`X-Accel-Buffering: no`（避免 Nginx 缓冲）。
- 流开始前若 `llm_configured()` 为 False 直接返回 503 JSON（此时流尚未建立）；中途失败发 `error` 帧。
- 用户消息在流开始前入库（`session` 帧带其 id）；assistant 消息在 `done` 前入库，中断/断开则不写入 assistant，前端保留已渲染文本可重发。
- 会话创建时 `account_id` 同前：用 `owned_account` 校验，不属于 → 404（在流开始前抛出）。

- `PlanSessionOut.plan_id`：该会话最新方案卡片消息的 `plan_id`（取 `plan_id` 非空且 id 最大的消息，列表用一条带子查询的 select 取出，避免 N+1），列表展示「已生成方案」。
- `PlanMessageOut`：`id, role, content, plan_id, created_at`；方案状态由前端用现有 `GET /api/plans/{plan_id}` 按需拉取（生成中每 3s 轮询，刷新后同样拉取）。
- `GET /api/plan-sessions/{id}` 最多返回最近 200 条消息，避免长会话拖慢页面。
- 会话不存在/不属于当前用户 → 404；`POST /api/plan-sessions` 的 `account_id` 用现有 `owned_account` 校验，不属于 → 404。
- `POST .../plan`：取该会话 `plan_id` 非空且 id 最大的消息，若对应 plan 仍为 `pending|running` → 400「方案生成中，请稍候」。无任何用户消息 → 400「先聊几句再生成」。
- 聊天接口 LLM 未配置 → 503（沿用 `_agent_call` 的 `LLMNotConfigured` 映射）；人格被停用 → 404（沿用 `PersonaUnavailable`）。

### 5.4 对话上下文与提示词

- 上下文 = 该会话最近 20 条 `plan_id is null` 的消息，拼成「用户：… / 你：…」+ 新消息，**不再使用 Redis**。`/api/agent/chat` 保持原样不动。
- 会话绑定账号时，首轮与每轮 prompt 前置：`账号：{nickname}（account_id={id}，粉丝 {followers}，赛道 {niche}）`；未绑定写「全新账号，暂无数据」。
- 新增 `agent_tasks.chat_in_session(db, user_id, persona_key, account, dialog, message)`：**不复用** `agent_tasks.chat`（Redis 版只有 8 轮、无会话维度）。对话约定不改 yaml，而是拼在发给模型的**用户消息前缀**里：
  - 信息不足时一次只问最关键的一个问题（赛道、目标、账号现状、每周可投入时间）。
  - 不要在对话里输出完整长方案；信息足够时提示用户点击下方「生成起号方案」。
  - 保持简洁，每条回复 ≤ 200 字（除用户明确要求展开）。
- 发送失败（`_agent_call` 抛 502 或流中 error 帧）：用户消息已入库并返回前端，不写空的 assistant 消息，用户可重发；前端保留输入内容并 toast。
- 每次写入用户或 assistant 消息后显式 `session.updated_at = datetime.now()`（更新的是另一张表，`onupdate` 不会自动触发）。

### 5.4.1 流式实现（AgentScope 2.0.11）

- `agents/runtime.py`：`_build_model(stream: bool = False)` 参数化（`OpenAIChatModel` 默认 `stream=True`），并新增：
  ```python
  async def run_agent_stream(persona, skills, prompt, user_id=None) -> AsyncIterator[str]:
      agent = Agent(..., model=_build_model(stream=True), ...)
      async for evt in agent.reply_stream(UserMsg(name="user", content=prompt)):
          if isinstance(evt, TextBlockDeltaEvent):
              yield evt.delta        # 忽略 ThinkingBlockDelta / 工具事件
  ```
- `agent_tasks.chat_in_session_stream(...)`：构造 prompt（同 5.4）→ `async for delta in run_agent_stream(...)` → `yield delta`。
- 流式端点**不能依赖请求级 `get_db`**（yield 依赖在响应发送前被关闭）：端点函数只做 `current_user` 鉴权与参数校验，generator 内部 `async with SessionLocal() as db:` 自行读写；事件生成与写库都在这个会话里。
- 工具调用期间（可能 10–30s）没有文本 delta，前端靠「思考中…」占位；一期不加服务端心跳，部署到 Nginx 时须配 `proxy_buffering off` / `proxy_read_timeout 300s`。

### 5.5 方案生成

新增 `agent_tasks.generate_plan_from_session(plan_id, session_id)`：

1. 读会话消息（plan_id 为空，最多 50 条）与 `GrowthPlan` 行。
2. 无 LLM：`content = _template_plan(plan.niche, plan.goal)`（与现有兜底一致）。
3. 有 LLM：prompt 含「咨询对话全文 + 账号上下文（若有则先查数据）+ 按起号方法论 6 部分输出、具体到每周动作」，`ask(db, plan.persona_key, prompt, plan.user_id)`。
4. 写 `status/content/error`，与现有 `generate_plan` 相同的状态机。

元信息提取 `agent_tasks.extract_plan_meta(db, user_id, dialog) -> (niche, goal)`：

- 用 `growth_coach` 人格 + 空技能列表跑一次 `run_agent`，prompt 要求只输出 `{"niche": "≤20字", "goal": "≤50字"}`。
- 从回复中取第一个 `{...}` 做 `json.loads`；失败回退 `(session.title[:64] or "起号", "AI 对话生成")`。两个值都截断到列宽。
- 提取失败不阻断生成。

`POST .../plan` 流程（点按钮不等 LLM，卡片立即出现）：

1. 占位建 `GrowthPlan(user_id, account_id=session.account_id, niche=session.title[:64] or "起号", goal="AI 对话生成", persona_key=session.persona_key)` → commit → 得到 plan_id。
2. 插入 assistant 方案消息 `{content:"", plan_id}`，显式更新 `session.updated_at`。
3. `BackgroundTasks(generate_plan_from_session(plan_id, session_id))`。
4. 立即返回 `{plan_id}`，前端每 3s 轮询方案状态。

后台任务内部顺序：先 `extract_plan_meta` 并更新 `plan.niche/goal`（失败保留占位值），再生成方案内容。

### 5.6 前端要点

- `plan.vue` 重写为双 segment 列表页：对话列表 `GET /api/plan-sessions`、方案列表沿用 `GET /api/plans`；访客仍可浏览（不登录时两个 tab 都提示登录，进入 chat 走 `goLogin`）。
- `api.js` 新增 `streamChat(body, { onSession, onDelta, onDone, onError, signal })`：
  - H5：`fetch(BASE + '/api/plan-sessions/chat', {method:'POST', headers, body, signal})` → `res.body.getReader()` + `TextDecoder`，按 `\n\n` 分帧解析 `event:` / `data:`。
  - 非 H5（App/小程序）：`fetch` 不可用时降级为 `uni.request` 一次性收取整个 SSE 文本，逐帧解析后一次性回放（无打字机效果，但不阻断）。
  - 非 2xx：解析 JSON `detail` 后抛错（沿用现有 toast 习惯）。
- `chat.vue`：
  - `onLoad(query)`：有 `id` 拉会话；无 `id` 走新建态（静态开场白 + 可选账号 picker）。
  - 发送：立即插入用户气泡 + 一个「思考中…」的 assistant 占位气泡 → 调 `streamChat`；收到 `session` 帧记录 `session_id`（新建态后续复用）；`delta` 累加到占位气泡；`done` 后落定并停止占位。渲染用 `mdToHtml`，按 ~50ms 节流刷新，避免逐字重渲染卡顿。
  - 生成方案：`POST .../plan` → 追加本地方案卡片（携带 plan_id）→ 每 3s `GET /api/plans/{plan_id}` 直至 done/failed；卡片点击 `navigateTo('/pages/plan/detail?id=')`。
  - 页面 `onUnload`：`AbortController.abort()` 中断流、清轮询定时器。
  - 高度用 `calc(100vh - var(--window-top) - var(--window-bottom))`（同 `agent.vue`）。
- `pages.json` 注册 `pages/plan/chat`（标题「起号专家」）。
- `detail.vue` 不变。

### 5.7 LLM 未配置降级

- `chat.vue` 进入时读 `GET /api/agent/status`；`llm_configured=false` → 顶部提示 + 禁用输入；展示「模板快速生成」卡（赛道、目标两个输入 → `POST /api/plans` → 跳 detail）。
- 列表与方案详情不受影响。

## 6. 迁移与兼容

- 两张新表由 `init_db` 的 `create_all` 自动创建（MySQL/SQLite 均适用），旧表零改动。
- `GrowthPlan`、`POST /api/plans`、`detail.vue`、admin 方案管理全部保留。
- 旧方案无会话关联：在 plan.vue 的「方案」tab 仍可见可点开；不会出现在对话列表。
- 历史 Redis 聊天键（`xhs:chat:*`）不受影响，`/api/agent/chat` 继续服务「AI 助手」tab。

## 7. 测试计划

新增 `backend/tests/test_plan_chat.py`（复用 `conftest` 的 `client/auth/create_user`）：

- monkeypatch `agent_tasks.chat_in_session_stream`（async generator，yield 固定片段）/ `agent_tasks.extract_plan_meta`；流式端点用 `httpx.AsyncClient.stream("POST", ...)` 读取并按帧断言（`session` → `delta`×N → `done`）。
- 首问创建会话：`session` 帧带 `session_id`、`title` 为消息截断、用户消息入库；账号不归属 → 404（流前抛出）。
- 流结束：assistant 消息入库且 `done` 帧内容一致；LLM 未配置 → 503。
- 续聊：消息累积、`updated_at` 前移；会话列表 `plan_id` 为空。
- 方案生成：无 LLM → `generate_plan_from_session` 模板兜底，`status=done`；会话插入 plan 消息；列表 `plan_id` 非空；`niche/goal` 被 mock 的 meta 覆盖；重复生成在未完成时 400（mock plan 状态验证）。
- 越权：别人的会话 GET/POST/DELETE → 404。
- 删除会话：列表消失，`GrowthPlan` 仍在。
- 现有 12 个测试保持通过；admin 统计不受影响。

## 8. 风险

- **同步等待**：ReAct `max_iters=8` + 工具调用，首字节可能 30–90s；SSE 建立后前端以「思考中…」占位，不阻塞连接。反向代理需关闭缓冲、放宽读超时（见 5.4.1）。
- **流中断**：客户端断开/超时时该轮 assistant 不落库（用户消息保留，可重发）；不做断点续传。
- **对话上下文长度**：20 条上限，长对话早期信息会丢；元信息提取与方案生成各读一次会话，成本可控。
- **同会话并发**：前端 `sending` 锁 + 生成中 400 兜底。

## 9. 留给实现者的决定

- 会话列表的具体视觉样式、空态文案。
- 消息轮询间隔（建议 3s）与停止条件。
- `extract_plan_meta` 解析失败时是否记录日志（建议记录，不影响返回）。
