# SDD ledger — plan: docs/superpowers/plans/2026-11-19-wechat-login.md

- Setup: 项目不是 git 仓库，`sdd-workspace` / `task-start` / `task-done` 脚本无法运行（要求 git 工作树与 commit）。
- Setup: Ruling: 手工维护本 ledger（同目录同格式），不做 worktree、不做 commit — 无 git 可用；代价：失去 commit 级回滚点，只能靠文件备份/测试兜底。
- Baseline: `cd backend && uv run pytest -q` → 54 passed, 65 warnings.
- Pre-flight: T1→T3 的 `build_provider`/`WeChatUser` 一致 ✓；T2→T3/T4 的 `new_state`/`read_state`/`issue_ticket`/`consume_ticket`/`TicketStoreError` 一致 ✓；T3→T4 同 router 追加 exchange ✓；T3→T5 接口路径与方法一致 ✓。无共享接口冲突。
- Task 1: complete (tests: `uv run pytest tests/test_wechat_login.py -v` → 5 passed; 全量 `uv run pytest -q` → 59 passed)
- Task 2: complete (tests: `uv run pytest tests/test_wechat_login.py -k "state or ticket"` → 4 passed)
- Task 3: complete (tests: `uv run pytest ... -k "methods or authorize or callback or reuse or tampered"` → 6 passed)
- Task 4: complete (tests: `uv run pytest tests/test_wechat_login.py -q` → 20 passed; 全量 `uv run pytest -q` → 74 passed)
- Task 5: complete (tests: `cd client && npm run build:h5` → DONE Build complete; dist 内无 appsecret/WECHAT_APP_SECRET)
- Task 6: complete (tests: 8001 实例端到端 — methods/authorize/callback/exchange/me 全通；二次 exchange 401；同一 mock openid 复用 user id=12；全量 `uv run pytest -q` → 75 passed)
- Final review: self-review (no subagent tool) — 逐文件通读 config/models/auth/{wechat,session}.py、user_api 新增段、login.vue、api.js，对照 spec 2/4.5/4.6/4.7 与 plan 的 Review Focus 逐条核对。
- Final: fixed provider 网络异常导致 500 裸 JSON — test_provider_network_error_redirects_to_login_error RED→GREEN，suite 75/75
- Final: minor (deferred): `CLIENT_BASE_URL` 末尾带斜杠会拼出 `//#/pages/...`，目前只在 .env.example 注释约束
- Final: minor (deferred): `consume_ticket` 对非数字 value 会 ValueError→500（值仅由本服务写入，风险低）
- Final: minor (deferred): live 部署需 uvicorn `--proxy-headers` 且反代传 `X-Forwarded-Proto`，否则 redirect_uri 为 http 与公众号授权域名不匹配；仅注释/报告提示，未做成配置项
- Final: Ruling: 保留本 ledger 目录不删除 — 项目无 git，删后无可回溯记录；代价：留下一个未被跟踪的 .superpowers/sdd 目录
- Final: Ruling: 跳过 finishing-a-development-branch — 无 git 仓库/分支可合并
