"""Plugin loading: personas (YAML files) and skills (directories).

Persona plugin  -> app/plugins/personas/<key>.yaml
    key, name, description, skills: [skill names], system_prompt

Skill plugin    -> app/plugins/skills/<name>/
    SKILL.md    front matter (name, description) + instructions for the agent
    tools.py    optional; `TOOLS = [async_fn, ...]`, each returns a str.
                Type hints + docstring become the tool schema.

Dropping a new file/directory in place is enough; operators switch plugins on/off
in the admin console (PluginState table). Users can also create personas in the DB.
"""

import contextvars
import importlib.util
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PLUGIN_DIR
from app.db import SessionLocal
from app.models import Persona, PluginState
from app.redis_client import get_redis
from app.services.skills import SKILLS_VERSION_KEY, list_skills, load_skill_files, materialize, split_front_matter

logger = logging.getLogger(__name__)

# Who the agent is acting for; skill tools read this to scope their queries.
current_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_user_id", default=None)


@dataclass
class PersonaSpec:
    key: str
    name: str
    system_prompt: str
    description: str = ""
    skills: list[str] = field(default_factory=list)
    builtin: bool = True
    owner_id: int | None = None
    is_public: bool = True


@dataclass
class SkillSpec:
    name: str
    description: str
    instructions: str
    path: Path | None = None  # built-in skills read from disk; uploaded ones live in files
    source: str = "builtin"  # builtin | uploaded
    files: dict[str, bytes] | None = None  # relative to the skill root, uploaded only
    file_count: int | None = None
    size: int | None = None

    @property
    def has_tools(self) -> bool:
        if self.files is not None:
            return "tools.py" in self.files
        return self.path is not None and (self.path / "tools.py").exists()

    def load_tools(self) -> list[Callable]:
        if self.files is not None:
            if "tools.py" not in self.files:
                return []
            base, digest = materialize(self.name, self.files)
            tools_file = base / "tools.py"
            module_name = f"xhs_skill_uploaded_{self.name}_{digest}"
        else:
            if self.path is None:
                return []
            tools_file = self.path / "tools.py"
            if not tools_file.exists():
                return []
            module_name = f"xhs_skill_{self.name}"
        spec = importlib.util.spec_from_file_location(module_name, tools_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return list(getattr(module, "TOOLS", []))


def load_builtin_skills() -> dict[str, SkillSpec]:
    skills = {}
    for skill_md in sorted((PLUGIN_DIR / "skills").glob("*/SKILL.md")):
        meta, body = split_front_matter(skill_md.read_text(encoding="utf-8"))
        name = meta.get("name") or skill_md.parent.name
        skills[name] = SkillSpec(name, meta.get("description", ""), body, skill_md.parent)
    return skills


# Version-stamped cache: every pod rebuilds when the Redis counter moves. The TTL is the
# safety net for a lost notification (INCR failed, or Redis blipped while the old value
# stayed readable) — without it a pod could serve a stale set until the next change.
SKILLS_CACHE_TTL_SECONDS = 30.0
_skills_cache: dict = {"version": None, "skills": {}, "expires_at": 0.0}


async def _skills_version() -> str:
    try:
        return await get_redis().get(SKILLS_VERSION_KEY) or "0"
    except Exception:
        # Redis down: fall back to a 3-second window, good enough for eventual consistency.
        return str(int(time.time() // 3))


async def load_all_skills() -> dict[str, SkillSpec]:
    """Built-in skills from disk plus operator-uploaded skills stored in the DB.

    Reads go through a fresh session: the caller's session may already hold an older
    snapshot (MySQL REPEATABLE READ), which would cache stale rows under a new version.
    """
    version = await _skills_version()
    cached = _skills_cache
    if cached["version"] == version and cached["skills"] and time.monotonic() < cached["expires_at"]:
        return dict(cached["skills"])

    builtin = load_builtin_skills()
    async with SessionLocal() as fresh:
        rows = await list_skills(fresh)
        files = await load_skill_files(fresh, [r.id for r in rows])
    uploaded = {
        r.name: SkillSpec(
            name=r.name,
            description=r.description,
            instructions=r.instructions,
            path=None,
            source="uploaded",
            files=files.get(r.id, {}),
            file_count=r.file_count,
            size=r.total_size,
        )
        for r in rows
    }
    merged = {**uploaded, **builtin}  # built-ins win on a name clash
    cached["version"], cached["skills"] = version, merged
    cached["expires_at"] = time.monotonic() + SKILLS_CACHE_TTL_SECONDS
    return dict(merged)


def load_builtin_personas() -> dict[str, PersonaSpec]:
    personas = {}
    for f in sorted((PLUGIN_DIR / "personas").glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        key = data.get("key") or f.stem
        personas[key] = PersonaSpec(
            key=key,
            name=data.get("name", key),
            description=data.get("description", ""),
            system_prompt=data["system_prompt"],
            skills=data.get("skills", []),
        )
    return personas


async def disabled_plugins(db: AsyncSession, kind: str) -> set[str]:
    rows = await db.scalars(select(PluginState).where(PluginState.kind == kind, PluginState.enabled.is_(False)))
    return {r.name for r in rows}


def _from_row(row: Persona) -> PersonaSpec:
    return PersonaSpec(
        key=row.key, name=row.name, description=row.description, system_prompt=row.system_prompt,
        skills=row.skills or [], builtin=False, owner_id=row.owner_id, is_public=row.is_public,
    )


async def list_personas(db: AsyncSession, user_id: int | None) -> list[PersonaSpec]:
    """Enabled built-ins + public custom personas + the user's own."""
    disabled = await disabled_plugins(db, "persona")
    result = [p for k, p in load_builtin_personas().items() if k not in disabled]
    cond = Persona.is_public.is_(True)
    if user_id is not None:
        cond = or_(cond, Persona.owner_id == user_id)
    result += [_from_row(r) for r in await db.scalars(select(Persona).where(cond))]
    return result


async def get_persona(db: AsyncSession, key: str, user_id: int | None) -> PersonaSpec | None:
    return next((p for p in await list_personas(db, user_id) if p.key == key), None)


async def skills_for(db: AsyncSession, persona: PersonaSpec) -> list[SkillSpec]:
    disabled = await disabled_plugins(db, "skill")
    available = await load_all_skills()
    return [available[n] for n in persona.skills if n in available and n not in disabled]
