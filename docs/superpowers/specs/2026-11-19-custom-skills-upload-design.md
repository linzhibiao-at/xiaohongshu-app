# 自定义 Skills 上传（数据库存储）设计

日期：2026-11-19
状态：待批准

## 1. 意图与原因

运营需要在不改代码、不重新发布镜像的前提下，通过管理后台把一个包含 `SKILL.md` 的技能文件夹（可附带 `tools.py`、`references/`、`assets/` 等资源）上传到平台，上传后立即出现在「Skills 插件」列表中，可启停，可绑定到人格，被 agent 使用。

必须在多 Pod 部署下成立：任意一个 Pod 收到上传，所有 Pod 都能看到同一份技能；Pod 重建后技能不丢。

现状不满足的原因：`app/agents/plugins.py` 的 `load_skills()` 扫描的是镜像内目录 `app/plugins/skills`，上传只能写本地磁盘，多 Pod 之间不共享，且容器可写层随 Pod 销毁。

## 2. 目标 / 非目标

目标：

- 上传的技能与内置技能在列表、启停、人格绑定、agent 调用上完全等价。
- 多 Pod 间最终一致（秒级），上传/删除无需重启、无需滚动发布。
- 上传的技能可删除；内置技能不可删除。
- 支持目录结构与二进制资源，支持 `tools.py`（管理员信任模型）。

非目标：

- 不把现有 8 个内置技能迁入数据库（继续随镜像发布）。
- 不做技能版本历史、回滚、多人协作编辑。
- 不做技能市场 / 分享 / 跨环境导入导出。
- 不为上传的 `tools.py` 提供沙箱隔离（见第 6 节安全边界）。

反目标（技术上能跑但算失败）：

- 上传后某些 Pod 看不到、或列表在不同 Pod 间跳变。
- 上传把内置技能覆盖或删除，导致平台基础能力丢失。
- 上传请求能写到 `plugins/` 之外的路径。

## 3. 约束

- `load_skills()` 目前是同步函数，被 `admin_api.plugins`、`admin_api.toggle_plugin`、`user_api._check_skills`、`user_api.skills`、`plugins.skills_for` 调用；其中 `skills_for` 已有 db 会话，其余调用点也都能拿到 db 会话。
- `SkillSpec.path` 是 `Path`，`load_tools()` 依赖 `<path>/tools.py` 存在；数据库技能没有磁盘路径。
- `has_tools` 目前用 `(s.path / "tools.py").exists()` 判断，`path` 为 None 时会报错，必须一并改。
- 启用状态已在 MySQL `plugin_states` 表（`kind=skill`），本身与 Pod 无关，不需改动。
- 项目无迁移框架，新表靠 `create_all` 自动创建；旧表补列靠 `db.py` 的 `_LATE_COLUMNS`。本设计只新增表，不改已有表结构。
- 鉴权：管理后台使用 `staff_user`（运营 + 管理员）依赖。
- 数据库为 MySQL（aiomysql），`max_allowed_packet` 默认 64MB 级别，单次上传不宜过大。

## 4. 方案对比

| 方案 | 做法 | 权衡 | 结论 |
|---|---|---|---|
| A 共享卷 | RWX 卷挂到 `plugins/skills/_custom` | 改动最小；依赖集群提供 RWX；挂整层会遮蔽镜像内置技能 | 集群不确定，放弃 |
| B 对象存储 + 本地缓存 | 上传写 S3/MinIO，各 Pod 定时同步 | 持久可靠；需要对象存储与同步/删除传播逻辑 | 依赖外部组件，放弃 |
| **C 数据库存储（选定）** | 技能元数据 + 全部文件存 MySQL，运行时按版本号重建内存缓存，`tools.py` 按需落到本地缓存目录再 import | Pod 无状态、一致性由 DB 保证；大文件占库、需要大小限制；每次请求多一次版本查询 | **采用** |

选 C 的原因：部署形态不依赖集群存储类型与外部对象存储；与现有「启用状态存 MySQL」的做法一致；删除与覆盖天然一致。

## 5. 设计

### 5.1 数据模型（新增两张表）

```python
class Skill(Base, TimestampMixin):
    __tablename__ = "skills"
    id: int
    name: str            # String(64), unique, index；= SKILL.md front matter 的 name
    description: str     # String(255)
    instructions: str    # SKILL.md 正文（去掉 front matter）
    created_by: int|None # FK users.id，上传者
    file_count: int
    total_size: int

class SkillFile(Base):
    __tablename__ = "skill_files"
    __table_args__ = (UniqueConstraint("skill_id", "path"),)
    id: int
    skill_id: int        # FK skills.id, index, ondelete CASCADE（ORM 用 delete-orphan 级联）
    path: str            # String(512)，相对技能根目录（SKILL.md 所在目录），统一用 "/" 分隔
    size: int
    content: bytes       # LargeBinary
```

- `SKILL.md` 本身也作为 `SkillFile` 保存（便于落盘后 `tools.py` 相对读取），`instructions` 只是解析后的副本，用于 prompt 与列表。
- 路径以技能根目录（`SKILL.md` 所在目录）为基：上传时的顶层目录前缀在入库前剥除，库内路径形如 `SKILL.md`、`tools.py`、`references/x.md`；同时保证所有文件都在同一技能目录下。
- 不新增启用状态列，继续用 `plugin_states`。

### 5.2 加载与合并

`app/agents/plugins.py` 调整：

```python
@dataclass
class SkillSpec:
    name: str
    description: str
    instructions: str
    path: Path | None = None          # 内置技能有；数据库技能为 None
    source: str = "builtin"           # builtin | uploaded
    files: dict[str, bytes] | None = None  # 数据库技能的完整文件集
```

- 保留现有磁盘扫描为 `load_builtin_skills()`（原 `load_skills()` 改名，行为不变）。
- 新增 `async def load_all_skills(db) -> dict[str, SkillSpec]`：内置 + 数据库合并，**同名以内置为准**（上传接口已拒绝同名，此处只是防御）。
- `SkillSpec.has_tools` 改为属性：内置看 `path/tools.py`，上传看 `"tools.py" in files`。
- 所有原 `load_skills()` 调用点改为 `await load_all_skills(db)`；`user_api._check_skills` 改为 `async def _check_skills(db, names)`（`admin_api.create_public_persona` 同步改）。

### 5.3 多 Pod 一致性

- 进程内缓存：`{"version": float, "skills": dict[str, SkillSpec]}`。
- 版本号来源：Redis key `xhs:skills:version`（上传/删除/覆盖成功后 `INCR`）。
- `load_all_skills` 每次调用先读版本号；版本一致且缓存非空则直接返回缓存，否则查库重建（进程启动时缓存为空，一定会重建一次）。
- Redis 不可用时降级：版本号取 `int(time.time() // 3)`（3 秒窗口），并打一次 warning。
- 效果：上传后其他 Pod 在下一个请求即生效；Redis 挂掉时最坏 3 秒。

### 5.4 `tools.py` 的落盘与导入

- 内置技能：行为不变，`importlib` 直接加载 `path/tools.py`。
- 数据库技能：把 `files` 写到一个进程级缓存目录 `{tempdir}/xhs_skills/{name}-{digest8}/`（`digest` 为全部文件内容哈希），然后走同样的 `spec_from_file_location`，模块名 `xhs_skill_uploaded_{name}_{digest8}` 避免重名冲突。
- 目录写一次即复用；内容变化后 digest 变化，自然生成新目录。
- 清理：进程启动时清理 `{tempdir}/xhs_skills/` 下的陈旧目录（保留当天创建的，删除更早的），避免长期运行磁盘堆积。
- 限制：缓存目录使用 `tempfile.gettempdir()`，容器内 `/tmp` 可写；不依赖镜像目录。

### 5.5 上传接口

```
POST /api/admin/skills        multipart/form-data
  files: list[UploadFile]     # 必填
  paths: list[str]            # 与 files 一一对应的相对路径
  overwrite: bool = false
```

校验顺序与规则：

1. 鉴权 `staff_user`。
2. `len(files) == len(paths)`，否则 400。
3. 路径规范化：替换 `\` 为 `/`，拒绝空段、绝对路径、任何 `..` 段、含控制字符的段；否则 400「非法路径」。
4. 忽略规则（后端也拒绝）：`.git/`、`.venv/`、`node_modules/`、`__pycache__/`、`.DS_Store`。
5. 必须存在 `SKILL.md`，否则 400。
6. 解析 front matter：`name` 缺失时取顶层目录名；`name` 需匹配 `^[a-z0-9][a-z0-9_-]{1,63}$`。
7. 与内置技能同名 → 409「与内置技能重名」；与数据库已有技能同名：`overwrite=false` → 409，`overwrite=true` → 整组替换（先删旧 `SkillFile` 再写新）。
8. 大小限制：单文件 ≤ 5MB，单技能合计 ≤ 20MB，文件数 ≤ 300；超限 400 并指明具体文件。
9. 写入 `skills` + `skill_files`，`audit(db, actor, "skill.upload", name)`，`INCR` 版本号，返回 `{name, file_count, total_size, replaced}`。

### 5.6 删除接口

```
DELETE /api/admin/skills/{name}
```

- 内置技能 → 400「内置技能不能删除」。
- 不存在 → 404。
- 删除 `skill_files` 与 `skills` 行；同时清理该技能的 `plugin_states` 行，避免垃圾状态。
- `audit(db, actor, "skill.delete", name)`，`INCR` 版本号。

### 5.7 列表接口

`GET /api/admin/plugins` 的 `skills` 项增加字段：`source`（builtin/uploaded）、`file_count`、`size`，`has_tools` 改用 `SkillSpec.has_tools`。

`POST /api/admin/plugins/toggle` 的 `known` 改为 `await load_all_skills(db)`，使上传技能也能启停。

`GET /api/skills`（C 端）同样改用 `load_all_skills(db)`，`has_tools` 用属性。

### 5.8 前端（admin/src/views/Plugins.vue）

- Skills 卡片 header 右侧加「上传 Skill 文件夹」按钮 + 隐藏的 `<input type="file" webkitdirectory multiple>`。
- 选择后按 `file.webkitRelativePath` 收集：`FormData.append('files', file, relPath)` 与 `FormData.append('paths', relPath)`；前端先过滤 5.5 的忽略目录与单文件大小，给出提示。
- 上传中按钮 loading；成功 `ElMessage.success` + 刷新列表。
- 遇到 409（同名）弹 `ElMessageBox.confirm` 询问是否覆盖，确认后以 `overwrite=true` 重发。
- 表格新增「来源」列（内置/上传）；上传来源的行显示删除按钮，确认后 `DELETE /api/admin/skills/{name}`。
- 上传按钮旁一行小字提示：上传目录中的 `tools.py` 会在服务器端执行，请仅上传可信内容。

### 5.9 安全边界（明确记录）

- 允许上传 `tools.py`：上传的 Python 会在每个处理请求的 Pod 进程内被 import 执行，等同于服务器任意代码执行；访问控制只有 `staff_user`（运营 + 管理员）。这是用户明确选择，不作为遗留风险重复讨论，但必须在 UI 与审计日志中可见。
- 上传代码只能用镜像内已安装的依赖；缺失依赖时工具调用会失败，不影响平台其他部分。
- 路径校验按 5.5 执行，保证不会写到 `plugins/` 之外；数据库技能本身不落盘到 `plugins/`。

### 5.10 迁移与兼容

- 新增表由 `init_db()` 的 `create_all` 自动创建，不改已有表，`_LATE_COLUMNS` 不需新增条目。
- 现有内置技能、`plugin_states`、人格绑定数据都不受影响。
- 回滚方式：删除新表 + 还原代码，内置技能仍可用。

### 5.11 测试（backend/tests/test_platform.py 或新增 test_skills.py）

1. 上传含 `SKILL.md` + `tools.py` 的目录 → 200；`GET /api/admin/plugins` 出现该技能，`source=uploaded`、`has_tools=true`。
2. `load_tools()` 能执行上传的 `tools.py`，返回预期字符串。
3. 用户可创建人格并绑定该技能（`POST /api/personas` 通过 `_check_skills`）。
4. 路径穿越（`../evil.py`）→ 400；无 `SKILL.md` → 400；超限 → 400。
5. 与内置同名 → 409；`overwrite=true` 可替换并更新内容。
6. `DELETE` 后列表消失、`skill_files` 清空、`plugin_states` 清理；删除内置 → 400。
7. 非 staff（普通用户）访问上传/删除 → 403。
8. 版本号变更后缓存失效：上传新技能后同一进程内下一次 `load_all_skills` 能看到。

## 6. 留给实现者的决定

- 缓存目录是否可通过配置项（如 `SKILL_CACHE_DIR`）覆盖，默认 `tempfile.gettempdir()`。
- `load_all_skills` 的 Redis 版本读取失败是否需要打日志级别控制。
- 上传时是否允许空目录结构（本设计：不允许，只处理文件）。
- 前端是否提供「下载/导出」按钮（本设计：不做）。

## 7. 实现期调整（审查后修订，以此为准）

- MySQL 列类型：`skills.instructions` 用 `MEDIUMTEXT`、`skill_files.content` 用 `MEDIUMBLOB`（`TEXT`/`BLOB` 只有 64KB，撑不起 5MB 上限）；`skill_files.path` 用 `utf8mb4_bin` 排序规则，否则 `A.md` 与 `a.md` 会撞唯一键。`init_db()` 的 `_upgrade_mysql_columns` 对已存在的表补 `ALTER TABLE`（仅 MySQL 方言，SQLite 测试不受影响）。
- 进程内缓存除版本号外还有 30 秒 TTL，兜底「INCR 失败 / 读到旧版本号」导致的永久陈旧；缓存重建用独立 `SessionLocal`，避免复用调用方已有的事务快照（MySQL REPEATABLE READ）。
- `load_all_skills()` 不接收 session；`save_skill`/`delete_skill` 只 flush 不 commit，由接口把技能、`plugin_states` 清理与审计一次性提交，提交后再 `bump_skills_version()`。
- 上传按 `read_uploads()` 边读边校验上限（单文件最多读 5MB+1），不先把整个请求读进内存；`materialize()` 先写临时目录再 `os.replace` 原子发布，缓存目录只在文件齐全时复用。
- 依赖：`pymysql` 约束 `>=1.0,<1.2`（1.2.x 删除了 `converters.escape_bytes_prefixed` 函数，aiomysql 0.3.2 仍按函数 import，导致一切 bytes 参数写入 MySQL 失败）。
