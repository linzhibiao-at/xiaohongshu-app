# 小红书笔记运营平台

| 目录 | 说明 | 技术 |
|---|---|---|
| `backend/` | API + 互动任务 worker + Agent | Python、FastAPI、AgentScope、MySQL、Redis、E2B |
| `client/` | C 端 web(H5) 与 App，同一套代码 | uni-app (Vue3) |
| `admin/` | 管理后台 web（运营 + 运维） | Vue3、Element Plus |

## 功能

- **起号涨粉方案**：填写赛道和目标，由「起号教练」人格结合起号方法论 skill（可选结合账号流量数据）生成落地方案。
- **互动任务**：对指定笔记点赞 / 收藏 / 评论。任务进 Redis 延时队列，由 worker 执行，带单账号每日上限和最小间隔。
  评论可由 AI 起草；默认每条评论要用户确认后才发出，账号可开启「评论免确认」。
- **流量分析**：同步笔记数据快照，计算点击率 / 互动率 / 涨粉率、按日趋势、按发布时段 / 标签 / 类型的表现，并可让「数据分析师」人格做诊断。
- **人格插件化**：`backend/app/plugins/personas/*.yaml`，放入文件即生效；用户可在 C 端自建人格，运营可在后台发布公共人格、启停内置人格。
- **Skills 插件化**：`backend/app/plugins/skills/<name>/SKILL.md`（说明书）+ 可选 `tools.py`（`TOOLS = [async 函数]`，返回 str）。后台可启停。
- **E2B 沙箱**：`data_sandbox` skill 让 agent 在隔离沙箱里跑 Python 做数据计算。
- **管理后台**：数据概览、用户管理（启停 / 角色）、方案与互动任务查看和取消、插件管理、系统健康与审计日志（仅管理员）。

## 小红书连接器（重要）

所有访问小红书的动作都经过 `backend/app/xhs/base.py` 的 `XhsConnector` 接口。**仓库只内置 `mock` 连接器**：
笔记数据是模拟生成的，点赞 / 收藏 / 评论不会真的发到小红书。

要接入真实数据和真实互动，需要你基于有权使用的渠道实现该接口并 `register_connector("名字", 类)`，再设置 `XHS_CONNECTOR`。
小红书没有面向个人账号开放点赞 / 评论的官方 API，本项目不包含逆向私有接口、签名破解或规避风控的实现；
用自动化脚本操作账号违反小红书用户协议，有限流和封号风险，接入前请自行评估。

## 启动

```bash
# 1. 基础设施 + 后端（MySQL、Redis、API、worker）
cp backend/.env.example backend/.env    # 改 JWT_SECRET、ADMIN_PASSWORD，填 LLM_API_KEY / E2B_API_KEY
docker compose up -d --build

# 2. 管理后台  http://localhost:5174   （用 .env 里的 ADMIN_USERNAME / ADMIN_PASSWORD 登录）
cd admin && npm install && npm run dev

# 3. C 端 H5   http://localhost:5173
cd client && npm install && npm run dev:h5
```

本地不用 Docker 跑后端（uv 工作流，依赖由 `pyproject.toml` + `uv.lock` 管理）：

```bash
cd backend
uv sync                                       # 首次 / 依赖变更后：按 uv.lock 创建 .venv 并安装依赖
cp .env.example .env                          # 首次：改 JWT_SECRET、ADMIN_PASSWORD，填 LLM_API_KEY / E2B_API_KEY

uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload   # API  → http://127.0.0.1:8000/docs
uv run python -m app.worker                                         # 互动任务 worker（不启动则任务一直排队）
uv run pytest                                                       # 测试（sqlite + fakeredis，不需要 MySQL/Redis）
```

几点必须注意：

- 需要 MySQL(3306) 与 Redis(6379)。已有容器时 `docker start <mysql容器> <redis容器>`，或用仓库自带的 `docker compose up -d mysql redis`。
- 配置由 `app/config.py` 按**相对当前工作目录**的 `.env` 读取，所以必须在 `backend` 目录下启动；表结构在启动时自动创建，无需手动建表。
- 改 `.env` 不会触发 `--reload`（watchfiles 只监听 `.py`），需重启进程。
- `requirements.txt` 是 `uv export` 生成的完整锁定清单、供 Dockerfile 使用；改动依赖后执行 `uv export --format requirements-txt --no-hashes --all-groups -o requirements.txt` 保持同步。

本地跑管理后台（Vue3 + Element Plus + Vite）：

```bash
cd admin
npm install                        # 首次 / 依赖变更后
npm run dev                        # → http://localhost:5174（用 backend/.env 的 ADMIN_USERNAME / ADMIN_PASSWORD 登录）
npm run build && npm run preview   # 生产构建 / 本地预览 dist
```

- 端口 5174 和 `/api` → `http://127.0.0.1:8000` 的代理写在 `vite.config.js` 的 `server` 里（可直接改），后端须先起在 8000。
- 同样只监听 IPv6 `[::1]`，用 `http://localhost:5174` 访问。

本地跑 C 端 H5（uni-app Vue3 + Vite，源码在 `client/src`）：

```bash
cd client
npm install                 # 首次 / 依赖变更后
npm run dev:h5              # → http://localhost:5173
npm run build:h5            # 产物在 dist/build/h5，可静态托管
```

- 端口 5173 和 `/api` → `http://127.0.0.1:8000` 的代理写在 `src/manifest.json` 的 `h5.devServer`（不在 `vite.config.js`），所以后端必须先起在 8000。
- dev server 只监听 IPv6 `[::1]`：用 `http://localhost:5173` 打开，`127.0.0.1:5173` 连不上；要手机 / 局域网访问加 `npm run dev:h5 -- --host`。
- `@dcloudio/*` 五个包必须钉在同一个 vue3 通道版本（当前 `3.0.0-alpha-5030720261117001`），`vite` 精确取 `5.2.8`（`vite-plugin-uni` 的 peer 是精确版本）。npm 的 `latest` tag 指向 vue2 老通道，不要按 `npm outdated` 提示升级。
- `src/App.vue` 是应用入口，只放 `onLaunch` 和全局样式，不能写 `<template>`（否则编译报 `At least one <template> or <script> is required`）。

App 端：`npm run dev:app` / `npm run build:app` 编译 app 资源；正式出包用 HBuilderX 导入 `client` 后「发行 → 原生 App 云打包」，
打包前设置 `VITE_API_BASE` 为后端的公网地址。

## 配置项（backend/.env）

| 变量 | 说明 |
|---|---|
| `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` | `anthropic`（默认）/ `dashscope` / `openai`。不填 key 时方案走内置模板，对话 / 诊断 / AI 评论不可用 |
| `E2B_API_KEY` | 不填则 `data_sandbox` 工具返回「沙箱未配置」 |
| `XHS_CONNECTOR` | 默认 `mock` |
| `INTERACTION_DAILY_LIMIT` / `INTERACTION_MIN_INTERVAL_SECONDS` | 单账号每日互动上限 / 两次互动最小间隔 |

## 写一个新插件

人格：新建 `backend/app/plugins/personas/my_persona.yaml`

```yaml
key: my_persona
name: 选题官
description: 每次给 3 个选题
skills: [growth_playbook]
system_prompt: |
  你是……
```

Skill：新建目录 `backend/app/plugins/skills/my_skill/`，写 `SKILL.md`（front matter 含 `name`、`description`），
需要工具时加 `tools.py`：

```python
async def my_tool(keyword: str) -> str:
    """工具说明。

    Args:
        keyword: 参数说明。
    """
    return "结果"

TOOLS = [my_tool]
```
