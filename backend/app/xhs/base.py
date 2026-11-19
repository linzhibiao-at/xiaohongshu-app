"""Xiaohongshu connector abstraction.

Everything that touches Xiaohongshu goes through `XhsConnector`. The platform ships
only the `mock` connector (simulated data, no network access). To go live, implement
this interface on top of a channel you are authorised to use (e.g. the official
开放平台 / 蒲公英 / 聚光 APIs, or data the account owner exports) and register it:

    from app.xhs.base import register_connector
    register_connector("my_channel", MyConnector)

then set XHS_CONNECTOR=my_channel.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class NoteSnapshot:
    xhs_note_id: str
    title: str
    note_type: str = "image"
    tags: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    impressions: int = 0
    views: int = 0
    likes: int = 0
    collects: int = 0
    comments: int = 0
    shares: int = 0
    new_followers: int = 0


class ConnectorError(Exception):
    pass


class XhsConnector(ABC):
    def __init__(self, credential: str = "") -> None:
        self.credential = credential

    @abstractmethod
    async def list_notes(self, account_ref: str) -> list[NoteSnapshot]:
        """Return the account's notes with their current traffic numbers."""

    async def get_note(self, note_ref: str) -> NoteSnapshot:
        """Read one public note by its id (see app.services.note_analysis).

        Optional: connectors that can only read the account owner's own data
        should keep this raising, and the link-analysis endpoint returns 502.
        """
        raise ConnectorError("当前连接器不支持按链接读取笔记")

    @abstractmethod
    async def like(self, target_note: str) -> None: ...

    @abstractmethod
    async def collect(self, target_note: str) -> None: ...

    @abstractmethod
    async def comment(self, target_note: str, text: str) -> None: ...


_registry: dict[str, type[XhsConnector]] = {}


def register_connector(name: str, cls: type[XhsConnector]) -> None:
    _registry[name] = cls


def get_connector(name: str, credential: str = "") -> XhsConnector:
    from app.xhs import mock  # noqa: F401  (registers "mock")

    if name not in _registry:
        raise ConnectorError(f"未注册的小红书连接器: {name}")
    return _registry[name](credential)


def connector_names() -> list[str]:
    from app.xhs import mock  # noqa: F401

    return sorted(_registry)
