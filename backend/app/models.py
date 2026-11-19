from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.mysql import MEDIUMBLOB, MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class TimestampMixin:
    # MySQL 的 INSERT 不返回 server_default 列，若只依赖 server_default，创建后序列化响应时
    # 会触发隐式 IO（异步会话下报 MissingGreenlet），因此应用侧同时给出默认值。
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())


class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), default="user")  # user | operator | admin
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Soft delete from the console: the account disappears while its data stays.
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class UserIdentity(Base, TimestampMixin):
    """第三方身份（provider + 该平台内唯一 id）到平台用户的绑定。"""

    __tablename__ = "user_identities"
    __table_args__ = (UniqueConstraint("provider", "provider_uid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16), default="wechat")  # wechat | xhs | ...
    provider_uid: Mapped[str] = mapped_column(String(128))  # 公众号 openid
    unionid: Mapped[str] = mapped_column(String(128), default="")
    nickname: Mapped[str] = mapped_column(String(64), default="")
    avatar: Mapped[str] = mapped_column(String(512), default="")


class XhsAccount(Base, TimestampMixin):
    """A Xiaohongshu account the user operates."""

    __tablename__ = "xhs_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    xhs_id: Mapped[str] = mapped_column(String(64), default="")  # 小红书账号（小红书号/账号标识）
    nickname: Mapped[str] = mapped_column(String(64))
    niche: Mapped[str] = mapped_column(String(64), default="")  # 赛道，如 美妆/穿搭/职场
    followers: Mapped[int] = mapped_column(Integer, default=0)
    connector: Mapped[str] = mapped_column(String(32), default="mock")
    # Opaque credential handed to the connector (e.g. an authorised session token).
    credential: Mapped[str] = mapped_column(Text, default="")
    auto_interaction: Mapped[bool] = mapped_column(Boolean, default=False)


class GrowthPlan(Base, TimestampMixin):
    __tablename__ = "growth_plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("xhs_accounts.id"), nullable=True)
    niche: Mapped[str] = mapped_column(String(64))
    goal: Mapped[str] = mapped_column(String(255))
    persona_key: Mapped[str] = mapped_column(String(64), default="growth_coach")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|running|done|failed
    content: Mapped[str] = mapped_column(Text, default="")  # markdown
    error: Mapped[str] = mapped_column(Text, default="")


class PlanSession(Base, TimestampMixin):
    """One consultation thread with the growth coach."""

    __tablename__ = "plan_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("xhs_accounts.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(64), default="")
    persona_key: Mapped[str] = mapped_column(String(64), default="growth_coach")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class PlanMessage(Base, TimestampMixin):
    """A chat turn; plan_id non-null marks a generated-plan card."""

    __tablename__ = "plan_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("plan_sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("growth_plans.id"), nullable=True)


class Note(Base, TimestampMixin):
    __tablename__ = "notes"
    __table_args__ = (UniqueConstraint("account_id", "xhs_note_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("xhs_accounts.id"), index=True)
    xhs_note_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    note_type: Mapped[str] = mapped_column(String(16), default="image")  # image | video
    tags: Mapped[list] = mapped_column(JSON, default=list)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class NoteMetric(Base):
    """Point-in-time snapshot of a note's traffic numbers."""

    __tablename__ = "note_metrics"
    id: Mapped[int] = mapped_column(primary_key=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    collects: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    new_followers: Mapped[int] = mapped_column(Integer, default=0)


class InteractionTask(Base, TimestampMixin):
    __tablename__ = "interaction_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("xhs_accounts.id"), index=True)
    target_note: Mapped[str] = mapped_column(String(512))  # note id or url
    action: Mapped[str] = mapped_column(String(16))  # like | collect | comment
    comment_text: Mapped[str] = mapped_column(Text, default="")
    # pending_approval | queued | running | done | failed | rejected | cancelled
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Persona(Base, TimestampMixin):
    """User/operator defined persona. Built-in ones live as YAML plugins on disk."""

    __tablename__ = "personas"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(255), default="")
    system_prompt: Mapped[str] = mapped_column(Text)
    skills: Mapped[list] = mapped_column(JSON, default=list)  # skill names this persona may use
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)


class Skill(Base, TimestampMixin):
    """Operator-uploaded skill kept in the database, files included."""

    __tablename__ = "skills"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    instructions: Mapped[str] = mapped_column(
        Text().with_variant(MEDIUMTEXT(), "mysql"), default=""
    )  # SKILL.md body, front matter stripped; MySQL TEXT would cap at 64KB
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    total_size: Mapped[int] = mapped_column(Integer, default=0)
    files: Mapped[list["SkillFile"]] = relationship(back_populates="skill", cascade="all, delete-orphan")


class SkillFile(Base):
    """One file of an uploaded skill; path is relative to the skill root."""

    __tablename__ = "skill_files"
    __table_args__ = (UniqueConstraint("skill_id", "path"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), index=True)
    # MySQL 默认按大小写不敏感比较，A.md 与 a.md 会撞唯一键，故指定二进制排序规则。
    path: Mapped[str] = mapped_column(String(512).with_variant(String(512, collation="utf8mb4_bin"), "mysql"))
    size: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[bytes] = mapped_column(LargeBinary().with_variant(MEDIUMBLOB(), "mysql"))
    skill: Mapped[Skill] = relationship(back_populates="files")


class PluginState(Base):
    """Admin on/off switch for on-disk plugins (personas and skills)."""

    __tablename__ = "plugin_states"
    __table_args__ = (UniqueConstraint("kind", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # persona | skill
    name: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
