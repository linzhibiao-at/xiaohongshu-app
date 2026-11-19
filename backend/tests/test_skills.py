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
    await db.commit()  # the service flushes; the caller owns the transaction
    row = next(r for r in await svc.list_skills(db) if r.name == "bin_skill")
    files = (await svc.load_skill_files(db, [row.id]))[row.id]
    assert files["SKILL.md"] == md and files["cover.png"] == png


async def test_load_all_skills_merges_db_skill(db):
    from app.agents.plugins import load_all_skills

    payload = svc.parse_upload(["merged_skill/SKILL.md"],
                               [b"---\nname: merged_skill\ndescription: m\n---\n\nbody\n"])
    await svc.save_skill(db, payload, created_by=None, overwrite=False)
    await db.commit()
    await svc.bump_skills_version()
    merged = await load_all_skills()
    assert "merged_skill" in merged and merged["merged_skill"].source == "uploaded"
    assert isinstance(merged["merged_skill"].files, dict)
    assert "data_sandbox" in merged  # 内置仍在


async def test_skills_for_ignores_missing_skill(db):
    from app.agents.plugins import PersonaSpec, skills_for

    persona = PersonaSpec(key="p", name="P", system_prompt="x", skills=["gone_skill"])
    assert await skills_for(db, persona) == []


async def test_check_skills_accepts_uploaded_skill(client, auth):
    from app.db import SessionLocal

    payload = svc.parse_upload(["user_skill/SKILL.md"], [b"---\nname: user_skill\n---\n\nb\n"])
    async with SessionLocal() as db:
        await svc.save_skill(db, payload, created_by=None, overwrite=False)
        await db.commit()
    await svc.bump_skills_version()  # direct service use skips the endpoint's notify step
    r = await client.post("/api/personas", headers=auth,
                          json={"name": "绑定上传技能", "system_prompt": "你是一个测试人格，回答要简短。",
                                "skills": ["user_skill"]})
    assert r.status_code == 200, r.text


def _upload_parts(name: str, files: dict[str, bytes]):
    # files 的键相对技能目录，例如 {"SKILL.md": ..., "tools.py": ...}
    paths = [f"{name}/{rel}" for rel in files]
    parts = [("files", (p, files[rel], "application/octet-stream")) for p, rel in zip(paths, files)]
    return paths, parts


async def _upload(client, admin, name: str, files: dict[str, bytes], overwrite: bool = False):
    paths, parts = _upload_parts(name, files)
    return await client.post("/api/admin/skills", headers=admin,
                             data={"paths": paths, "overwrite": str(overwrite).lower()}, files=parts)


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
    mismatch = await client.post("/api/admin/skills", headers=admin,
                                 data={"paths": ["a/SKILL.md", "a/b.md"], "overwrite": "false"},
                                 files=[("files", ("a/SKILL.md", b"---\nname: a1\n---\nb", "text/plain"))])
    assert mismatch.status_code == 400
    forbidden = await client.post("/api/admin/skills", headers=auth,
                                  data={"paths": ["a/SKILL.md"]},
                                  files=[("files", ("a/SKILL.md", b"---\nname: a1\n---\nb", "text/plain"))])
    assert forbidden.status_code == 403
    assert (await client.delete("/api/admin/skills/data_sandbox", headers=admin)).status_code == 400


async def test_overwrite_replaces_old_files(client, admin):
    # 覆盖后只应剩下新上传的文件（旧 old.md 必须消失）
    await _upload(client, admin, "over_skill",
                  {"SKILL.md": b"---\nname: over_skill\n---\nv1\n", "old.md": b"old"})
    r = await _upload(client, admin, "over_skill",
                      {"SKILL.md": b"---\nname: over_skill\n---\nv2\n"}, overwrite=True)
    assert r.status_code == 200 and r.json()["replaced"] is True

    from app.db import SessionLocal

    async with SessionLocal() as db:
        row = next(x for x in await svc.list_skills(db) if x.name == "over_skill")
        files = (await svc.load_skill_files(db, [row.id]))[row.id]
    assert set(files) == {"SKILL.md"} and b"v2" in files["SKILL.md"]


_TOOLS = b'''async def demo_tool() -> str:
    """Return a fixed marker."""
    return "demo-ok"


TOOLS = [demo_tool]
'''


async def test_uploaded_skill_tools_are_importable(client, admin):
    await _upload(client, admin, "tool_skill",
                  {"SKILL.md": b"---\nname: tool_skill\n---\nbody\n", "tools.py": _TOOLS})
    from app.agents.plugins import load_all_skills

    spec = (await load_all_skills())["tool_skill"]
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


def test_mysql_ddl_fits_uploaded_skill_sizes():
    """MySQL TEXT/BLOB cap out at 64KB, far below the 5MB upload limit; paths must be case sensitive."""
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateTable

    from app.models import Skill, SkillFile

    skills_ddl = str(CreateTable(Skill.__table__).compile(dialect=mysql.dialect()))
    files_ddl = str(CreateTable(SkillFile.__table__).compile(dialect=mysql.dialect()))
    assert "MEDIUMTEXT" in skills_ddl
    assert "MEDIUMBLOB" in files_ddl
    assert "utf8mb4_bin" in files_ddl


def test_parse_upload_rejects_non_mapping_front_matter():
    with pytest.raises(svc.SkillUploadError):
        svc.parse_upload(["bad/SKILL.md"], [b"---\n- name: demo\n---\nbody"])


async def test_save_skill_leaves_commit_and_version_bump_to_caller(db):
    from app.redis_client import get_redis

    before = await get_redis().get(svc.SKILLS_VERSION_KEY)
    payload = svc.parse_upload(["quiet_skill/SKILL.md"], [b"---\nname: quiet_skill\n---\nb\n"])
    await svc.save_skill(db, payload, created_by=None, overwrite=False)
    assert await get_redis().get(svc.SKILLS_VERSION_KEY) == before
    await db.commit()


async def test_save_skill_reports_unique_key_race_as_conflict(db, monkeypatch):
    """A racing pod inserted the same name after our existence check: 409, not a 500."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db import SessionLocal

    payload = svc.parse_upload(["dup_skill/SKILL.md"], [b"---\nname: dup_skill\n---\nb\n"])
    async with SessionLocal() as other:
        await svc.save_skill(other, payload, created_by=None, overwrite=False)
        await other.commit()

    async def no_rows(*args, **kwargs):
        return None

    monkeypatch.setattr(AsyncSession, "scalar", no_rows)
    with pytest.raises(svc.SkillConflictError):
        await svc.save_skill(db, payload, created_by=None, overwrite=False)


async def test_save_skill_overwrite_recovers_from_race(db, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db import SessionLocal

    payload = svc.parse_upload(["race_skill/SKILL.md"], [b"---\nname: race_skill\n---\nv2\n"])
    async with SessionLocal() as other:
        await svc.save_skill(other, payload, created_by=None, overwrite=False)
        await other.commit()

    real = AsyncSession.scalar
    calls = {"n": 0}

    async def flaky(self, *args, **kwargs):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalar", flaky)
    assert await svc.save_skill(db, payload, created_by=None, overwrite=True) is True
    await db.commit()


async def test_load_all_skills_does_not_need_the_caller_session():
    """The request session may hold an old REPEATABLE READ snapshot; reads use a fresh one."""
    from app.agents.plugins import load_all_skills

    assert "data_sandbox" in await load_all_skills()


async def test_skills_cache_expires_when_notification_is_lost():
    from app.agents import plugins

    version = await plugins._skills_version()
    plugins._skills_cache.update(version=version, skills={"stale": object()}, expires_at=0.0)
    merged = await plugins.load_all_skills()
    assert "stale" not in merged and "data_sandbox" in merged


class _FakeUpload:
    def __init__(self, size: int):
        self.size, self.calls = size, []

    async def read(self, n: int = -1) -> bytes:
        self.calls.append(n)
        return b"x" * (min(n, self.size) if n >= 0 else self.size)


async def test_read_uploads_stops_at_the_limit_instead_of_buffering():
    big = _FakeUpload(svc.MAX_FILE_BYTES + 10)
    with pytest.raises(svc.SkillUploadError):
        await svc.read_uploads([big], ["big/SKILL.md"])
    assert big.calls == [svc.MAX_FILE_BYTES + 1]

    second = _FakeUpload(10)
    with pytest.raises(svc.SkillUploadError):
        await svc.read_uploads([big, second], ["big/SKILL.md", "big/b.md"])
    assert second.calls == []


def test_materialize_recovers_from_a_partial_write(monkeypatch, tmp_path):
    from pathlib import Path

    monkeypatch.setattr(svc, "CACHE_ROOT", tmp_path / "cache")  # 独立目录，避免旧缓存干扰
    files = {"tools.py": b"x", "refer/a.md": b"y"}

    def boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", boom)
    with pytest.raises(OSError):
        svc.materialize("broken_skill", files)
    monkeypatch.undo()

    base, _ = svc.materialize("broken_skill", files)
    assert (base / "tools.py").read_bytes() == b"x"
    assert (base / "refer/a.md").read_bytes() == b"y"

