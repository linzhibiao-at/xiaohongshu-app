"""Custom skill storage: parse an uploaded folder, persist it, invalidate caches.

A skill folder is expected to contain a ``SKILL.md`` (front matter + instructions).
Everything else (``tools.py``, references, assets) is stored as-is. Paths kept in
the DB are relative to the skill root — the folder that holds ``SKILL.md`` — so a
browser that sends ``my_skill/SKILL.md`` and ``my_skill/tools.py`` stores
``SKILL.md`` and ``tools.py``.
"""

import hashlib
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from tempfile import gettempdir, mkdtemp

import yaml
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Skill, SkillFile
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILES = 300
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
# Folders/names produced by tooling, never part of a skill.
IGNORED_PARTS = {".git", ".venv", "node_modules", "__pycache__"}
IGNORED_NAMES = {".DS_Store"}

SKILLS_VERSION_KEY = "xhs:skills:version"
CACHE_ROOT = Path(gettempdir()) / "xhs_skills"


class SkillUploadError(ValueError):
    """The uploaded folder is not a usable skill (HTTP 400)."""


class SkillConflictError(ValueError):
    """A skill with the same name already exists (HTTP 409)."""


@dataclass
class SkillPayload:
    name: str
    description: str
    instructions: str
    files: dict[str, bytes]  # relative to the skill root
    file_count: int
    total_size: int


def split_front_matter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            meta = yaml.safe_load(parts[1]) or {}
            return meta, parts[2].strip()
    return {}, text.strip()


def normalize_path(raw: str) -> str:
    """Return a safe relative path or raise; rejects traversal and control chars."""
    path = raw.replace("\\", "/")
    if path.startswith("/"):
        raise SkillUploadError(f"非法路径: {raw}")
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", "..") or any(ord(c) < 32 for c in part):
            raise SkillUploadError(f"非法路径: {raw}")
    return "/".join(parts)


def _ignored(path: str) -> bool:
    parts = path.split("/")
    return any(p in IGNORED_PARTS for p in parts[:-1]) or parts[-1] in IGNORED_NAMES


def parse_upload(paths: list[str], contents: list[bytes]) -> SkillPayload:
    if len(paths) != len(contents):
        raise SkillUploadError("paths 与 files 数量不一致")

    kept: dict[str, bytes] = {}
    for raw, data in zip(paths, contents):
        path = normalize_path(raw)
        if _ignored(path):
            continue
        if len(data) > MAX_FILE_BYTES:
            raise SkillUploadError(f"文件超过 5MB 限制: {path}")
        kept[path] = data
    if len(kept) > MAX_FILES:
        raise SkillUploadError(f"文件数量超过 {MAX_FILES} 个限制")
    if sum(len(v) for v in kept.values()) > MAX_TOTAL_BYTES:
        raise SkillUploadError("技能总大小超过 20MB 限制")

    skill_md_path = next((p for p in kept if p == "SKILL.md" or p.endswith("/SKILL.md")), None)
    if skill_md_path is None:
        raise SkillUploadError("上传目录必须包含 SKILL.md")
    try:
        text = kept[skill_md_path].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillUploadError("SKILL.md 必须是 UTF-8 文本") from exc
    try:
        meta, instructions = split_front_matter(text)
    except yaml.YAMLError as exc:
        raise SkillUploadError("SKILL.md 的 front matter 不是合法 YAML") from exc
    if not isinstance(meta, dict):
        raise SkillUploadError("SKILL.md 的 front matter 必须是 YAML 映射")

    root = skill_md_path.rsplit("/", 1)[0] if "/" in skill_md_path else ""
    if root:
        prefix = root + "/"
        files = {}
        for path, data in kept.items():
            if not path.startswith(prefix):
                raise SkillUploadError("所有文件必须位于同一技能目录")
            files[path[len(prefix) :]] = data
    else:
        files = dict(kept)

    name = str(meta.get("name") or root.rsplit("/", 1)[-1]).strip()
    if not NAME_RE.match(name):
        raise SkillUploadError("技能名只能用小写字母、数字、下划线或短横线，至少 2 位")
    return SkillPayload(
        name=name,
        description=str(meta.get("description") or "")[:255],
        instructions=instructions,
        files=files,
        file_count=len(files),
        total_size=sum(len(v) for v in files.values()),
    )


async def read_uploads(files, paths: list[str]) -> list[bytes]:
    """Read the uploads, enforcing the limits while reading instead of after buffering.

    ``files`` are Starlette uploads (``await f.read(n)``). Ignored paths are not read at all;
    every other file is read with a hard cap, so an oversized request never lands in memory.
    """
    if len(paths) != len(files):
        raise SkillUploadError("paths 与 files 数量不一致")

    contents: list[bytes] = []
    kept = 0
    total = 0
    for raw, upload in zip(paths, files):
        path = normalize_path(raw)
        if _ignored(path):
            contents.append(b"")
            continue
        data = await upload.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise SkillUploadError(f"文件超过 5MB 限制: {path}")
        kept += 1
        if kept > MAX_FILES:
            raise SkillUploadError(f"文件数量超过 {MAX_FILES} 个限制")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise SkillUploadError("技能总大小超过 20MB 限制")
        contents.append(data)
    return contents


async def save_skill(
    db: AsyncSession, payload: SkillPayload, created_by: int | None, overwrite: bool
) -> bool:
    """Insert the skill (optionally replacing a same-named one) and flush.

    The caller owns the transaction, so the skill, its audit entry and any plugin-state
    cleanup commit or roll back together. Returns whether an existing skill was replaced.
    """
    for attempt in (0, 1):
        row = await db.scalar(select(Skill).where(Skill.name == payload.name))
        if row is not None and not overwrite:
            raise SkillConflictError(payload.name)
        replaced = row is not None
        try:
            if row is not None:
                await db.delete(row)
                await db.flush()
            skill = Skill(
                name=payload.name,
                description=payload.description,
                instructions=payload.instructions,
                created_by=created_by,
                file_count=payload.file_count,
                total_size=payload.total_size,
            )
            db.add(skill)
            await db.flush()
            for path, content in payload.files.items():
                db.add(SkillFile(skill_id=skill.id, path=path, size=len(content), content=content))
            await db.flush()
        except IntegrityError as exc:
            # Another pod inserted the same name between our check and our insert.
            await db.rollback()
            if attempt:
                raise SkillConflictError(payload.name) from exc
            continue
        return replaced
    raise SkillConflictError(payload.name)


async def delete_skill(db: AsyncSession, name: str) -> bool:
    """Delete the skill and flush; the caller commits."""
    row = await db.scalar(select(Skill).where(Skill.name == name))
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def list_skills(db: AsyncSession) -> list[Skill]:
    return list(await db.scalars(select(Skill).order_by(Skill.name)))


async def load_skill_files(db: AsyncSession, skill_ids: list[int]) -> dict[int, dict[str, bytes]]:
    if not skill_ids:
        return {}
    files: dict[int, dict[str, bytes]] = {}
    for row in await db.scalars(select(SkillFile).where(SkillFile.skill_id.in_(skill_ids))):
        files.setdefault(row.skill_id, {})[row.path] = row.content
    return files


async def bump_skills_version() -> None:
    """Tell every pod the skill set changed; a failure only delays visibility."""
    try:
        await get_redis().incr(SKILLS_VERSION_KEY)
    except Exception:
        logger.warning("更新 skills 版本号失败，其他节点可能延迟看到变更", exc_info=True)


def _complete(base: Path, files: dict[str, bytes]) -> bool:
    """A cache dir is reusable only when every expected file is there at the right size."""
    if not base.is_dir():
        return False
    for relpath, content in files.items():
        target = base / relpath
        if not target.is_file() or target.stat().st_size != len(content):
            return False
    return True


def materialize(name: str, files: dict[str, bytes]) -> tuple[Path, str]:
    """Write a skill's files to a content-addressed cache dir and return (dir, digest).

    The files are built in a temp dir and renamed into place, so a crash mid-write never
    leaves a directory that later calls mistake for a complete one.
    """
    digest = hashlib.sha256(
        "".join(f"{p}\0{hashlib.sha256(c).hexdigest()}" for p, c in sorted(files.items())).encode()
    ).hexdigest()[:8]
    base = CACHE_ROOT / f"{name}-{digest}"
    if _complete(base, files):
        return base, digest

    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = Path(mkdtemp(prefix=f".{name}-{digest}-", dir=CACHE_ROOT))
    try:
        for relpath, content in files.items():
            target = tmp / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        shutil.rmtree(base, ignore_errors=True)  # legacy or incomplete leftover
        try:
            os.replace(tmp, base)
        except OSError:
            if not _complete(base, files):  # lost the race to a bad dir; rebuild next time
                raise
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
    return base, digest


def cleanup_cache_dir() -> None:
    """Drop cache dirs left by earlier processes (keep today's)."""
    if not CACHE_ROOT.exists():
        return
    today = date.today()
    for child in CACHE_ROOT.iterdir():
        try:
            if child.is_dir() and date.fromtimestamp(child.stat().st_mtime) < today:
                for path in sorted(child.rglob("*"), reverse=True):
                    path.unlink() if path.is_file() else path.rmdir()
                child.rmdir()
        except OSError:
            logger.warning("清理技能缓存目录失败: %s", child, exc_info=True)
