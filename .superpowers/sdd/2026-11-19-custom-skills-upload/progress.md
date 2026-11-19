# SDD ledger — plan: docs/superpowers/plans/2026-11-19-custom-skills-upload.md

Setup: 项目不是 git 仓库（`git rev-parse` 失败），SDD 脚本（sdd-workspace/task-start/task-done/review-package）不可用，也无 worktree 隔离。
Ruling: 手工维护本 ledger，不以 commit 作为任务边界，改用「任务级测试通过」作为检查点；无 git 意味着无法按 commit range 做最终 review，最终审查改为对照计划与 spec 自查 + 独立 reviewer 读改动文件。若日后 `git init`，可从本 ledger 恢复任务顺序。代价：无回滚点，误改只能靠人工比对还原。

Pre-flight: Task 2 消费 Task 1 的 `list_skills` / `load_skill_files` / `SkillPayload.files`；Task 3 消费 Task 1 的 `parse_upload`/`save_skill`/`delete_skill` 与 Task 2 的 `load_builtin_skills`；Task 4 消费 `SkillSpec.files` 与 `materialize`。三处接口在计划 Interfaces 块中签名一致，`files` 键均为相对技能根目录（已核对）。无冲突。

Task 1: complete (no git; tests: `uv run pytest tests/test_skills.py -q` → 7 passed; full suite `uv run pytest -q` → 31 passed)
Task 2: complete (no git; `uv run pytest tests/test_skills.py -q` → 10 passed; full suite → 34 passed; grep load_skills 残留 0)
Task 3: complete (no git; `uv run pytest tests/test_skills.py -q` → 13 passed; full suite → 37 passed)
Task 3: Ruling: 测试用 httpx 0.28 的 `data=` 只能是 Mapping（list-of-tuples 会被当成 raw content 并触发 sync-stream 报错），改为 `data={"paths": [...], "overwrite": ...}` + `files=[...]` 重复字段由 MultipartStream 展开 — 与后端 `list[str] = Form(...)` 兼容 — 若日后重启 httpx 版本需重验。
Task 4: complete (no git; `uv run pytest tests/test_skills.py -q` → 15 passed; full suite → 39 passed)
Task 4: Ruling: `materialize`/`cleanup_cache_dir` 在 Task 1 已随 services 一并落地（同文件辅助函数），故 materialize 测试首次运行即通过（未能观察失败）；load_tools 的 RED 仍有效（TypeError: NoneType / str）。代价：materialize 缺少「先红后绿」证据，后续若有改动缺回归基线，已由该测试固定当前行为。
Task 5: complete (no git; `npm --prefix admin run build` → built in 10.03s, Plugins-BGgMV3ck.js emitted)
Task 6: Ruling: 真实 MySQL 下 `POST /api/admin/skills` 500，根因是 PyMySQL 1.2.x 把 `converters.escape_bytes_prefixed` 改成哨兵字符串 `"DO NOT IMPORT THIS!!!"`，而 aiomysql 0.3.2（最新版）仍按函数 import，导致一切 bytes 参数转义失败（LargeBinary 是项目首个落 bytes 的列，单元测试用 SQLite 无法暴露）— 处置：`pyproject.toml` 增加 `pymysql>=1.0,<1.2`，`uv lock` 锁到 1.1.3 并重生成 `requirements.txt` — 代价：停留在 PyMySQL 1.1.x，等 aiomysql 适配 1.2 后应解禁。
Task 6: complete (no git; `uv run pytest -q` → 39 passed；双实例冒烟：8011 上传 200 → Redis 版本 None→1 → 8012 列表 source=uploaded/has_tools=true → 8012 `/api/skills` 可见 → 8012 删除 200 → 两实例均消失；审计 `skill.upload`/`skill.delete` 各一条；两实例日志无 500/MissingGreenlet)

Final review: fresh CodeReview subagent（explicit target，无 git diff，直读文件）→ 1 Critical + 8 Important，无 Minor。逐项复现后全部接受，一次修复通道：
Final: fixed MySQL TEXT/BLOB 仅 64KB，5MB 上传必然 500 — models 改 `MEDIUMTEXT`/`MEDIUMBLOB` + `utf8mb4_bin` path，`db.init_db` 新增 `_upgrade_mysql_columns` 对已有表 ALTER（仅 mysql 方言）— `test_mysql_ddl_fits_uploaded_skill_sizes` RED→GREEN；真实 MySQL 确认 information_schema 为 mediumtext/mediumblob+utf8mb4_bin，326KB 上传成功。
Final: fixed 缓存通知丢失后永久陈旧 — 加 `SKILLS_CACHE_TTL_SECONDS = 30` 兜底过期 — `test_skills_cache_expires_when_notification_is_lost` RED→GREEN。
Final: fixed 用调用方事务快照缓存（REPEATABLE READ 陈旧读）— `load_all_skills()` 去掉 db 参数，内部用独立 `SessionLocal` 读 — `test_load_all_skills_does_not_need_the_caller_session` RED→GREEN。
Final: fixed 并发同名上传返回 500 — `save_skill` 捕 `IntegrityError` → rollback → overwrite 时重试一次，否则 `SkillConflictError`(409) — `test_save_skill_reports_unique_key_race_as_conflict`、`test_save_skill_overwrite_recovers_from_race` RED→GREEN。
Final: fixed 技能变更/审计/PluginState 两次提交非原子 — 服务层只 flush，接口统一 commit 后再 `bump_skills_version()` — `test_save_skill_leaves_commit_and_version_bump_to_caller` RED→GREEN（直接调服务的测试补 commit + bump）。
Final: fixed 先全量读入内存再校验大小 — 新增 `read_uploads` 边读边限流（单文件读上限 MAX+1，超限即止）— `test_read_uploads_stops_at_the_limit_instead_of_buffering` RED→GREEN。
Final: fixed materialize 目录存在≠写完（残缺目录被永久复用）— 临时目录写入 + `os.replace` 原子发布 + `_complete()` 校验 — `test_materialize_recovers_from_a_partial_write` RED→GREEN。
Final: fixed front matter 为列表/标量时 AttributeError 500 — `parse_upload` 校验必须是 YAML 映射 — `test_parse_upload_rejects_non_mapping_front_matter` RED→GREEN。
Final: 修复后 `uv run pytest -q` → 48 passed（39 → 48）；`uv pip check` 全部兼容；双实例大数据冒烟通过（70KB SKILL.md + 256KB 二进制 + A.md/a.md 共存 + tools.py，跨实例可见与删除均 ok）。
Final: Ruling: 运行期缓存回收只做启动清理（spec 原选择）— 长跑 Pod 的 `/tmp/xhs_skills` 会累积内容哈希目录，靠 Pod 重建回收 — 若磁盘紧张需另加配额或定期清理任务。
Final: Ruling: 上传不携带符号链接（multipart 只有字节，落盘路径已由 `normalize_path` 限死），共享目录的 UID/权限属部署范畴，本批不改。
Final: Ruling: 人格按技能名绑定，删除后名字保留、同名重传即恢复（与既有 persona 绑定机制一致）— 代价：删除期间人格静默少一个技能。
Final: Ruling: 上传技能只承诺「SKILL.md + 可选 tools.py(TOOLS 列表)」契约，不缓存 sys.modules（模块名带 digest，每次重新执行）— 包内相对导入不支持。
Final: Ruling: 依赖面已核对 pyproject/uv.lock/requirements 均为 pymysql 1.1.3，`uv pip check` 无冲突，无需额外动作。
Final: Ruling: 不删除本 plan 的 workspace（`.superpowers/sdd/2026-11-19-custom-skills-upload/`）—— 项目非 git 仓库，本 ledger 是唯一变更记录，删了就无迹可查 — 若日后 `git init` 且提交完成，可删。
Final: spec 追加「7. 实现期调整」：MySQL 列类型/排序规则、缓存 TTL、事务边界、边读边限流、materialize 原子发布、pymysql 约束，供后续维护者参考。
