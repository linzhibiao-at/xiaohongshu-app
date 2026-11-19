# 自定义 Skills 上传（数据库存储）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 管理后台可上传一个含 `SKILL.md` 的技能文件夹（可带 `tools.py` 与资源），内容存 MySQL，所有 Pod 在秒级内一致可见、可启停、可绑定人格、可删除。

**Architecture:** 新增 `skills` / `skill_files` 两张表作为事实源；`load_all_skills(db)` 合并磁盘内置技能与数据库技能，用进程内缓存 + Redis 版本号 `xhs:skills:version` 做跨 Pod 失效；数据库技能的 `tools.py` 在首次使用时按内容哈希落到 `{tempdir}/xhs_skills/` 再 `importlib` 导入。

**Tech Stack:** FastAPI、SQLAlchemy 2.0 async、MySQL(aiomysql)/SQLite(aiosqlite)、Redis(fakeredis 测试)、pytest + httpx、Vue3 + Element Plus + axios。

**Spec:** `docs/superpowers/specs/2026-11-19-custom-skills-upload-design.md`

## Global Constraints

- 单文件 ≤ 5MB，单技能合计 ≤ 20MB，文件数 ≤ 300。
- 忽略并拒绝：`.git/`、`.venv/`、`node_modules/`、`__pycache__/`、`.DS_Store`。
- 技能名正则 `^[a-z0-9][a-z0-9_-]{1,63}$`；缺省取 `SKILL.md` front matter 的 `name`，再缺省取顶层目录名。
- 内置技能不可删除；与内置同名上传返回 409。
- 同名上传：`overwrite=false` → 409；`overwrite=true` → 整组替换（旧 `skill_files` 全部删除）。
- 版本号 key `xhs:skills:version`；Redis 不可用时版本取 `int(time.time() // 3)`。
- 缓存目录 `{tempdir}/xhs_skills/{name}-{digest8}/`，模块名 `xhs_skill_uploaded_{name}_{digest8}`。
- 上传/删除接口鉴权 `staff_user`，审计动作 `skill.upload` / `skill.delete`。
- 路径校验：`\` 归一为 `/`；拒绝绝对路径、空段、任何 `..` 段、控制字符。
- **本项目不是 git 仓库**：每个任务以「运行测试通过」作为检查点，不执行 `git commit`。
- 测试命令统一在 `backend/` 下运行：`uv run pytest -q`。

## Review Focus

1. `SKILL.md` 非 UTF-8 或 front matter 不是合法 YAML：应返回 400 中文提示，不能冒 500。
2. `paths` 与 `files` 数量不一致（伪造请求或浏览器异常）：应 400，不能 `IndexError`。
3. 技能被删除后仍被人格引用：`skills_for` 应静默忽略，不能抛异常。
4. 覆盖上传时旧文件未清理干净：新旧文件混在一起，属静默数据污染。
5. 二进制资源（png）上传后内容字节受损：图片类技能会静默失效。

## File Structure

- `backend/app/models.py`：新增 `Skill`、`SkillFile` 两个 ORM 模型。
- `backend/app/services/skills.py`（新建）：技能上传解析、校验、存取、版本号、缓存目录落盘。所有「技能文件长什么样、怎么校验」的规则集中在这里。
- `backend/app/agents/plugins.py`：`SkillSpec` 扩展、`load_builtin_skills()`、`load_all_skills(db)`、`has_tools` 属性、`load_tools()` 支持内存文件。
- `backend/app/api/admin_api.py`：`GET /api/admin/plugins` 增加来源字段；新增上传/删除接口；toggle 与人格校验改用合并后的技能集合。
- `backend/app/api/user_api.py`：`_check_skills` 改为异步带 db；`/api/skills` 改用合并集合。
- `admin/src/views/Plugins.vue`：上传文件夹按钮、来源列、删除按钮、409 覆盖确认。
- `backend/tests/conftest.py`：新增 `db` fixture。
- `backend/tests/test_skills.py`（新建）：本功能全部测试。
- `backend/tests/test_platform.py`：只改 `load_skills` 的导入名。

---

### Task 1: 数据模型与技能服务

**Files:**
- Modify: `backend/app/models.py`（在 `Persona` 之后追加）
- Create: `backend/app/services/skills.py`
- Modify: `backend/app/agents/plugins.py:61-65`（`_split_front_matter` 迁出，改为导入）
- Modify: `backend/tests/conftest.py`（新增 `db` fixture）
- Test: `backend/tests/test_skills.py`

**Interfaces:**
- Consumes: `app.db.Base`、`TimestampMixin`、`app.redis_client.get_redis()`
- Produces:
  - `class Skill(Base, TimestampMixin)`：`id`、`name: str(64) unique index`、`description: str(255)`、`instructions: Text`、`created_by: int|None`、`file_count: int`、`total_size: int`；`files` 关系 + `cascade="all, delete-orphan"`
  - `class SkillFile(Base)`：`id`、`skill_id: int FK skills.id index`、`path: str(512)`、`size: int`、`content: LargeBinary`；`UniqueConstraint("skill_id", "path")`
  - `class SkillUploadError(ValueError)` / `class SkillConflictError(ValueError)`
  - `@dataclass class SkillPayload`：`name: str`、`description: str`、`instructions: str`、`files: dict[str, bytes]`（键相对技能根目录，即 `SKILL.md` 所在目录，不含顶层目录前缀）、`file_count: int`、`total_size: int`
  - `def split_front_matter(text: str) -> tuple[dict, str]`
  - `def normalize_path(raw: str) -> str`
  - `def parse_upload(paths: list[str], contents: list[bytes]) -> SkillPayload`
  - `async def save_skill(db, payload: SkillPayload, created_by: int | None, overwrite: bool) -> bool`（返回是否替换了旧记录；内部 commit 并 `bump_skills_version()`）
  - `async def delete_skill(db, name: str) -> bool`（返回是否存在；内部 commit 并 `bump_skills_version()`）
  - `async def list_skills(db) -> list[Skill]`
  - `async def load_skill_files(db, skill_ids: list[int]) -> dict[int, dict[str, bytes]]`
  - `SKILLS_VERSION_KEY = "xhs:skills:version"`、`async def bump_skills_version() -> None`

- [ ] **Step 1: 写失败的测试**

```python
# backend/tests/test_skills.py
import pytest

from app.services import skills as svc

_MD = b"---\nname: demo_skill\ndescription: demo\n---\n\n# Demo body\n"


def test_parse_upload_extracts_front_matter():
    payload = svc.parse_upload(["demo_skill/SKILL.md"], [_MD])
    assert payload.name == "demo_skill"
    assert payload.description == "demo"
    assert "# Demo body" in payload.instructions
    assert payload.file_count == 1 and payload.total_size == len(_MD)


def test_parse_upload_rejects_traversal():
    with pytest.raises(svc.SkillUploadError):
        svc.parse_upload(["../evil.py"], [b"x"])


def test_parse_upload_requires_skill_md():
    with pytest.raises(svc.SkillUploadError):
        svc.parse_upload(["demo/a.md"], [b"x"])


def test_parse_upload_rejects_non_utf8_skill_md():
    with pytest.raises(svc.SkillUploadError):
        svc.parse_upload(["bad/SKILL.md"], [b"\xff\xfe\x00bad"])


def test_parse_upload_rejects_bad_front_matter():
    with pytest.raises(svc.SkillUploadError):
        svc.parse_upload(["bad/SKILL.md"], [b"---\nname: [unclosed\n---\nbody"])


def test_parse_upload_enforces_file_size_limit():
    with pytest.raises(svc.SkillUploadError):
        svc.parse_upload(["big/SKILL.md"], [_MD, b"x" * (svc.MAX_FILE_BYTES + 1)])


async def test_save_and_reload_files_including_binary(db):
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    md = b"---\nname: bin_skill\n---\n\nbody\n"
    payload = svc.parse_upload(["bin_skill/SKILL.md", "bin_skill/cover.png"], [md, png])
    await svc.save_skill(db, payload, created_by=None, overwrite=False)
    row = next(r for r in await svc.list_skills(db) if r.name == "bin_skill")
    files = (await svc.load_skill_files(db, [row.id]))[row.id]
    assert files["SKILL.md"] == md and files["cover.png"] == png
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_skills.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.services.skills'`

- [ ] **Step 3: 实现模型与 `app/services/skills.py`**

`Skill` 增加 `files: Mapped[list["SkillFile"]] = relationship(back_populates="skill", cascade="all, delete-orphan")`，`SkillFile` 反向外键 `skill: Mapped[Skill] = relationship(back_populates="files")`。

服务模块要点（签名见 Interfaces，实体逻辑如下）：
- `normalize_path`：`raw.replace("\\", "/")` 后按 `/` 分段；任一段为 `""`、`"."`、`".."`，或含 `ord(c) < 32`，或首段以 `/` 开头 → `SkillUploadError("非法路径: ...")`。
- `parse_upload`：先比 `len(paths) != len(contents)` → `SkillUploadError("paths 与 files 数量不一致")`；逐条 `normalize_path`，命中忽略目录/文件名 → 直接丢弃（不报错）；`len(contents) > MAX_FILES` → 报错；单文件 `len > MAX_FILE_BYTES` → 报错；累计 `> MAX_TOTAL_BYTES` → 报错；找 `endswith("/SKILL.md")` 或等于 `SKILL.md` 的文件，`decode("utf-8")` 失败 → `SkillUploadError("SKILL.md 必须是 UTF-8 文本")`；`split_front_matter` 的 YAML 解析异常 → `SkillUploadError("SKILL.md 的 front matter 不是合法 YAML")`；`name` 依次取 front matter、顶层目录名，`re.match(r"^[a-z0-9][a-z0-9_-]{1,63}$")` 失败 → `SkillUploadError`。
- 技能根目录：`SKILL.md` 所在目录。所有文件必须位于该目录之下，否则 `SkillUploadError("所有文件必须位于同一技能目录")`；入库时剥掉这段前缀，`files` 键变为 `SKILL.md`、`tools.py`、`references/x.md`。
- `save_skill`：查同名行；存在且 `overwrite=False` → `SkillConflictError(name)`；存在且覆盖 → `await db.delete(row)`（cascade 删文件）；再插入新 `Skill` + 每条 `SkillFile`（`size=len(content)`），`await db.commit()`，`await bump_skills_version()`，返回 `replaced`。
- `bump_skills_version`：`await get_redis().incr(SKILLS_VERSION_KEY)`，`except Exception` 只 `logger.warning`。

`plugins.py` 中删除 `_split_front_matter`，改为 `from app.services.skills import split_front_matter`，`load_skills` 内调用点同步改名。

`conftest.py` 增加：

```python
from app.db import SessionLocal

@pytest_asyncio.fixture
async def db():
    async with SessionLocal() as s:
        yield s
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_skills.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 跑全量回归**

Run: `uv run pytest -q`
Expected: 现有 12 个用例仍通过

---

### Task 2: 加载层合并与调用点迁移

**Files:**
- Modify: `backend/app/agents/plugins.py:32-58`（`SkillSpec`）、`:68-74`（`load_skills` 改名）、`:119-122`（`skills_for`）
- Modify: `backend/app/api/user_api.py:547-596`（`_check_skills` / `skills()` / 两个 persona 接口）
- Modify: `backend/app/api/admin_api.py:13`、`:238-258`（plugins）、`:267-279`（toggle）、`:291`（`_check_skills` 调用）
- Modify: `backend/tests/test_platform.py:1`、`:234`
- Test: `backend/tests/test_skills.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `list_skills`、`load_skill_files`、`Skill` 模型
- Produces:
  - `@dataclass class SkillSpec`：`name`、`description`、`instructions`、`path: Path | None = None`、`source: str = "builtin"`、`files: dict[str, bytes] | None = None`、`file_count: int | None = None`、`size: int | None = None`；`@property has_tools -> bool`
  - `def load_builtin_skills() -> dict[str, SkillSpec]`（原 `load_skills` 改名，行为不变）
  - `async def load_all_skills(db: AsyncSession) -> dict[str, SkillSpec]`（内置 + 数据库，同名内置优先，带版本缓存）
  - `async def _check_skills(db: AsyncSession, names: list[str]) -> None`（user_api，改为异步）
  - `GET /api/admin/plugins` 的 skills 项新增 `source`、`file_count`、`size`；`has_tools` 改用属性

- [ ] **Step 1: 写失败的测试**

```python
async def test_load_all_skills_merges_db_skill(db):
    from app.agents.plugins import load_all_skills

    payload = svc.parse_upload(["merged_skill/SKILL.md"],
                               [b"---\nname: merged_skill\ndescription: m\n---\n\nbody\n"])
    await svc.save_skill(db, payload, created_by=None, overwrite=False)
    merged = await load_all_skills(db)
    assert "merged_skill" in merged and merged["merged_skill"].source == "uploaded"
    assert isinstance(merged["merged_skill"].files, dict)
    assert "data_sandbox" in merged  # 内置仍在


async def test_skills_for_ignores_missing_skill(db):
    from app.agents.plugins import PersonaSpec, skills_for

    persona = PersonaSpec(key="p", name="P", system_prompt="x", skills=["gone_skill"])
    assert await skills_for(db, persona) == []


async def test_check_skills_accepts_uploaded_skill(client, auth):
    payload = svc.parse_upload(["user_skill/SKILL.md"], [b"---\nname: user_skill\n---\n\nb\n"])
    from app.db import SessionLocal

    async with SessionLocal() as db:
        await svc.save_skill(db, payload, created_by=None, overwrite=False)
    r = await client.post("/api/personas", headers=auth,
                          json={"name": "绑定上传技能", "system_prompt": "你是一个测试人格，回答要简短。",
                                "skills": ["user_skill"]})
    assert r.status_code == 200, r.text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_skills.py -q`
Expected: FAIL，`load_all_skills` 不存在 / `_check_skills` 签名不符

- [ ] **Step 3: 实现加载层与调用点迁移**

`SkillSpec.has_tools`：

```python
@property
def has_tools(self) -> bool:
    if self.files is not None:
        return "tools.py" in self.files
    return self.path is not None and (self.path / "tools.py").exists()
```

`load_all_skills`：先取版本号（`await get_redis().get(SKILLS_VERSION_KEY) or "0"`，异常时 `str(int(time.time() // 3))`）；与 `_cache["version"]` 相同且缓存非空 → 返回副本；否则 `load_builtin_skills()` + `await list_skills(db)` + `await load_skill_files(db, ids)`，构造 `SkillSpec(..., path=None, source="uploaded", files=..., file_count=r.file_count, size=r.total_size)`，合并 `{**uploaded, **builtin}`，写缓存后返回副本。

调用点：`admin_api.plugins` 用 `await load_all_skills(db)` 并为每项输出 `source`/`file_count`/`size`（内置两个字段为 `None`）；`toggle_plugin` 的 skill 分支改 `await load_all_skills(db)`；`create_public_persona` 改 `await _check_skills(db, body.skills)`；`user_api._check_skills(db, names)` 用 `await load_all_skills(db)`；`/api/skills` 用合并集合与 `s.has_tools`；`create_persona`/`update_persona` 传 db；`plugins.skills_for` 用 `await load_all_skills(db)`。

`test_platform.py` 的 `load_skills` 导入与第 234 行调用改为 `load_builtin_skills`。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest -q`
Expected: PASS（含新增 3 个）

---

### Task 3: 上传与删除接口

**Files:**
- Modify: `backend/app/api/admin_api.py`（导入 `File`、`Form`、`UploadFile`；在 plugins 区块后新增两个路由）
- Test: `backend/tests/test_skills.py`（追加）

**Interfaces:**
- Consumes: `parse_upload`、`save_skill`、`delete_skill`、`SkillUploadError`、`SkillConflictError`、`load_builtin_skills`、`load_all_skills`、`audit`
- Produces:
  - `POST /api/admin/skills`（multipart：`files: list[UploadFile]`、`paths: list[str]`、`overwrite: bool = False`）→ `{"name", "file_count", "total_size", "replaced"}`
  - `DELETE /api/admin/skills/{name}` → `{"deleted": 1}`

- [ ] **Step 1: 写失败的测试**

```python
def _multipart(name: str, files: dict[str, bytes]):
    paths = [f"{name}/{p}" for p in files]
    return {"data": {"paths": paths}, "files": [("files", (p, c, "application/octet-stream")) for p, c in zip(paths, files.values())]}


async def _upload(client, admin, name: str, files: dict[str, bytes], overwrite: bool = False):
    # files 的键相对技能目录，例如 {"SKILL.md": ..., "tools.py": ...}
    part = _multipart(name, files)
    return await client.post("/api/admin/skills", headers=admin,
                             data={**part["data"], "overwrite": str(overwrite).lower()}, files=part["files"])


async def test_upload_appears_in_plugin_list_and_can_be_deleted(client, admin, auth):
    r = await _upload(client, admin, "uploaded_skill",
                      {"SKILL.md": b"---\nname: uploaded_skill\ndescription: up\n---\n\nbody\n"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "uploaded_skill" and r.json()["replaced"] is False

    row = next(s for s in (await client.get("/api/admin/plugins", headers=admin)).json()["skills"]
               if s["name"] == "uploaded_skill")
    assert row["source"] == "uploaded" and row["has_tools"] is False

    names = {s["name"] for s in (await client.get("/api/skills", headers=auth)).json()}
    assert "uploaded_skill" in names

    assert (await client.delete("/api/admin/skills/uploaded_skill", headers=admin)).status_code == 200
    assert "uploaded_skill" not in {s["name"] for s in (await client.get("/api/skills", headers=auth)).json()}


async def test_upload_validation_and_conflicts(client, admin, auth):
    assert (await _upload(client, admin, "no_md", {"a.md": b"x"})).status_code == 400
    assert (await _upload(client, admin, "data_sandbox",
                          {"SKILL.md": b"---\nname: data_sandbox\n---\nb\n"})).status_code == 409
    assert (await client.post("/api/admin/skills", headers=admin,
                              data={"paths": ["a/SKILL.md", "a/b.md"], "overwrite": "false"},
                              files=[("files", ("a/SKILL.md", b"---\nname: a1\n---\nb", "text/plain"))]
                              )).status_code == 400
    assert (await client.post("/api/admin/skills", headers=auth,
                              data={"paths": ["a/SKILL.md"]},
                              files=[("files", ("a/SKILL.md", b"---\nname: a1\n---\nb", "text/plain"))])).status_code == 403
    assert (await client.delete("/api/admin/skills/data_sandbox", headers=admin)).status_code == 400


async def test_overwrite_replaces_old_files(client, admin):
    # 覆盖后只应剩下新上传的文件（旧 old.md 必须消失）
    await _upload(client, admin, "over_skill",
                  {"SKILL.md": b"---\nname: over_skill\n---\nv1\n", "old.md": b"old"})
    r = await _upload(client, admin, "over_skill",
                      {"SKILL.md": b"---\nname: over_skill\n---\nv2\n"}, overwrite=True)
    assert r.status_code == 200 and r.json()["replaced"] is True

    from app.db import SessionLocal
    from app.services import skills as svc

    async with SessionLocal() as db:
        row = next(x for x in await svc.list_skills(db) if x.name == "over_skill")
        files = (await svc.load_skill_files(db, [row.id]))[row.id]
    assert set(files) == {"SKILL.md"} and b"v2" in files["SKILL.md"]
```

`test_overwrite_replaces_old_files` 覆盖 Review Focus 4（旧文件残留）。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_skills.py -q -k "upload or overwrite or delete"`
Expected: FAIL，404（路由不存在）

- [ ] **Step 3: 实现两个路由**

`POST /api/admin/skills`：读取 `[await f.read() for f in files]`；`paths` 数量不一致 → 400「paths 与 files 数量不一致」；`parse_upload` 抛 `SkillUploadError` → 400 `str(exc)`；`payload.name in load_builtin_skills()` → 409「与内置技能重名」；`save_skill` 抛 `SkillConflictError` → 409「已存在同名技能，确认覆盖后重试」；成功后 `audit(db, actor, "skill.upload", f"{name} files={file_count}")` 再 `await db.commit()`（`save_skill` 已 commit，审计行单独 commit）；返回如 Interfaces。

`DELETE /api/admin/skills/{name}`：`name in load_builtin_skills()` → 400「内置技能不能删除」；`delete_skill` 返回 False → 404；删除 `PluginState(kind="skill", name=name)` 行；`audit` + commit；返回 `{"deleted": 1}`。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_skills.py -q`
Expected: PASS

---

### Task 4: `tools.py` 落盘与导入

**Files:**
- Modify: `backend/app/services/skills.py`（新增 `materialize` / `cleanup_cache_dir` / `CACHE_ROOT`）
- Modify: `backend/app/agents/plugins.py:51-58`（`load_tools`）
- Modify: `backend/app/main.py`（lifespan 启动时调用 `cleanup_cache_dir()`）
- Test: `backend/tests/test_skills.py`（追加）

**Interfaces:**
- Consumes: `SkillSpec.files`
- Produces:
  - `CACHE_ROOT: Path`（`Path(tempfile.gettempdir()) / "xhs_skills"`）
  - `def materialize(name: str, files: dict[str, bytes]) -> tuple[Path, str]`（返回目录与 8 位 digest）
  - `def cleanup_cache_dir() -> None`（删除非当天创建的目录）
  - `SkillSpec.load_tools()` 对 `files is not None` 的技能返回文件中的 `TOOLS`

- [ ] **Step 1: 写失败的测试**

```python
_TOOLS = b'''async def demo_tool() -> str:
    """Return a fixed marker."""
    return "demo-ok"


TOOLS = [demo_tool]
'''


async def test_uploaded_skill_tools_are_importable(client, admin):
    await _upload(client, admin, "tool_skill",
                  {"SKILL.md": b"---\nname: tool_skill\n---\nbody\n", "tools.py": _TOOLS})
    from app.agents.plugins import load_all_skills
    from app.db import SessionLocal

    async with SessionLocal() as db:
        spec = (await load_all_skills(db))["tool_skill"]
    assert spec.has_tools is True
    fns = spec.load_tools()
    assert [f.__name__ for f in fns] == ["demo_tool"]
    assert await fns[0]() == "demo-ok"


def test_materialize_writes_files_and_is_stable():
    files = {"tools.py": _TOOLS, "refer/x.md": b"ref"}
    first, digest = svc.materialize("tool_skill", files)
    second, digest2 = svc.materialize("tool_skill", files)
    assert first == second and digest == digest2
    assert (first / "tools.py").read_bytes() == _TOOLS
    assert (first / "refer/x.md").read_bytes() == b"ref"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_skills.py -q -k "tools or materialize"`
Expected: FAIL，`materialize` 不存在 / `load_tools` 对无 path 的技能报错

- [ ] **Step 3: 实现 `materialize` 与 `load_tools`**

`materialize`：`digest = sha256("".join(f"{p}\0{sha256(c).hexdigest()}" for p, c in sorted(files.items()))).hexdigest()[:8]`；目录 `CACHE_ROOT / f"{name}-{digest}"`；已存在直接返回；否则逐条 `(target / relpath).parent.mkdir(parents=True, exist_ok=True)` 后写字节（`relpath` 已由 `normalize_path` 保证安全）。

`load_tools`：`files` 非空时取 `materialize(self.name, self.files)` 的目录与 digest，`tools_file = base / "tools.py"`，模块名 `xhs_skill_uploaded_{self.name}_{digest}`；其余沿用现有 `spec_from_file_location` 逻辑。钩子在 `app/main.py` 的 lifespan 启动时调用一次 `cleanup_cache_dir()`（放 `init_db()` 之后，异常只记日志）。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_skills.py -q`
Expected: PASS

---

### Task 5: 管理后台上传交互

**Files:**
- Modify: `admin/src/views/Plugins.vue`

**Interfaces:**
- Consumes: `POST /api/admin/skills`、`DELETE /api/admin/skills/{name}`、`GET /api/admin/plugins` 的 `source` 字段
- Produces: 无（纯前端）

- [ ] **Step 1: 实现上传与删除**

Skills 卡片 header 改为标题 + 右侧按钮组：

```vue
<div style="display: flex; justify-content: space-between; align-items: center">
  <span>Skills 插件（内置 + 上传）</span>
  <div>
    <el-button type="primary" size="small" :loading="uploading" @click="folderInput.click()">上传 Skill 文件夹</el-button>
    <input ref="folderInput" type="file" webkitdirectory multiple style="display: none" @change="onFolderPicked" />
  </div>
</div>
```

上传逻辑：`onFolderPicked(e)` 取 `Array.from(e.target.files)`，过滤路径中含 `.git/`、`.venv/`、`node_modules/`、`__pycache__/` 或名为 `.DS_Store` 的项，以及 `size > 5 * 1024 * 1024` 的项（有过滤时先 `ElMessage.warning` 说明被跳过数量）；用 `FormData` 逐项 `append('files', file, file.webkitRelativePath)` 与 `append('paths', file.webkitRelativePath)`，再 `append('overwrite', 'false')`；`await http.post('/api/admin/skills', fd)` 成功后 `ElMessage.success` + `load()`；`catch` 中若 `err.response?.status === 409`，用 `ElMessageBox.confirm(\`已存在同名技能，是否覆盖？\`, '覆盖确认')` 确认后重发（`overwrite=true`）；最后 `e.target.value = ''`。`uploading` ref 包住整个过程。

表格新增「来源」列（`row.source === 'uploaded' ? '上传' : '内置'`），「操作」列宽度改 140，追加上传技能的删除按钮：`<el-button v-if="row.source === 'uploaded'" link type="danger" @click="removeSkill(row)">删除</el-button>`；`removeSkill` 先 `ElMessageBox.confirm`，再 `await http.delete(\`/api/admin/skills/${row.name}\`)` + `load()`。

按钮下方加一行 `<div class="hint">上传目录中的 tools.py 会在服务器端执行，请仅上传可信内容。</div>`，样式 `.hint { color: #909399; font-size: 12px; margin-top: 8px; }`。

- [ ] **Step 2: 编译验证**

Run: `npm --prefix admin run build`
Expected: 构建成功，无 `Plugins.vue` 相关报错

---

### Task 6: 端到端与多进程一致性验证

**Files:**
- 无代码改动（验证任务）

- [ ] **Step 1: 后端全量测试**

Run: `cd backend && uv run pytest -q`
Expected: 全部通过（现有 12 + 新增约 10）

- [ ] **Step 2: 真实 MySQL 双实例冒烟**

在 `backend/` 下起两个实例（同一个 `.env` 指向 MySQL 与 Redis）：

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8011 &
uv run uvicorn app.main:app --host 127.0.0.1 --port 8012 &
```

准备一个临时目录 `/tmp/skill_smoke/SKILL.md`（`---\nname: smoke_skill\ndescription: smoke\n---\n\nbody\n`）与 `/tmp/skill_smoke/tools.py`（返回 `"smoke-ok"`）。

- 在 8011 登录管理后台，`curl -F "paths=smoke_skill/SKILL.md" -F "files=@/tmp/skill_smoke/SKILL.md" ...` 上传；
- 在 **8012** 调 `GET /api/skills`，断言返回含 `smoke_skill`（证明跨进程一致，不依赖本机磁盘）；
- `redis-cli -n 2 GET xhs:skills:version` 的值变大（`INCR` 生效）；
- 在 8012 删除 `smoke_skill`，在 8011 列表确认已消失。

Expected: 两个实例的列表一致；删除后都消失。验证完 `kill` 两个实例并删除临时技能。

- [ ] **Step 3: 观察后端日志**

Expected: 无 `MissingGreenlet`、无未处理异常；上传/删除各有一条 `skill.upload` / `skill.delete` 审计记录（`GET /api/admin/audit-logs` 可见）。

---

## Self-Review 记录

- 覆盖 spec 各节：数据模型（Task 1）、加载与合并（Task 2）、多 Pod 一致性（Task 2 + 6）、tools 落盘（Task 4）、上传/删除接口（Task 3）、列表字段（Task 2）、前端（Task 5）、迁移兼容（Task 1，`create_all` 自动建表）、测试清单（Task 1-4）。
- Review Focus 的五条均对应到测试：非 UTF-8 / 非法 YAML（Task 1）、数量不匹配（Task 3）、删除后引用（Task 2）、覆盖残留（Task 3）、二进制完整（Task 1）。
- 类型一致性：`SkillSpec.files: dict[str, bytes] | None` 在 Task 1/2/4 一致；`load_builtin_skills` 在 Task 2/3/4 同名引用；`save_skill(db, payload, created_by, overwrite) -> bool` 全程一致。
- 非 git 仓库：所有任务不含 `git commit`，检查点为测试与构建。
