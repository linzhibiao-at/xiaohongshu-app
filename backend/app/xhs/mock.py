"""Simulated connector: deterministic fake notes, interactions are no-ops.

Lets the whole platform run end to end without touching Xiaohongshu.
"""

import hashlib
import random
from datetime import datetime, timedelta

from app.xhs.base import ConnectorError, NoteSnapshot, XhsConnector, register_connector

_TITLES = [
    "新手必看｜3步搞定{}",
    "{}避坑指南，看完少走一年弯路",
    "一周实测｜{}到底值不值",
    "被问爆的{}清单，直接抄作业",
    "{}干货合集，建议收藏",
    "普通人做{}的真实一天",
    "我是怎么靠{}涨到1000粉的",
    "{}的5个误区，第3个最常见",
]
_TAGS = ["干货分享", "新手入门", "避坑", "好物推荐", "日常vlog", "经验分享", "测评", "教程"]


class MockConnector(XhsConnector):
    async def list_notes(self, account_ref: str) -> list[NoteSnapshot]:
        seed = int(hashlib.sha256(account_ref.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        topic = account_ref.split(":")[-1] or "小红书运营"
        today = datetime.now().replace(minute=0, second=0, microsecond=0)
        # Numbers grow a little every day so repeated syncs produce a trend.
        growth = 1 + (today.toordinal() % 30) / 60
        notes = []
        for i in range(12):
            impressions = int(rng.randint(800, 30000) * growth)
            views = int(impressions * rng.uniform(0.06, 0.28))
            likes = int(views * rng.uniform(0.02, 0.12))
            notes.append(
                NoteSnapshot(
                    xhs_note_id=f"mock{seed:x}{i:02d}",
                    title=_TITLES[i % len(_TITLES)].format(topic),
                    note_type="video" if rng.random() < 0.3 else "image",
                    tags=rng.sample(_TAGS, 3),
                    published_at=today - timedelta(days=i * 2 + 1, hours=-rng.choice([8, 12, 18, 21]) + today.hour),
                    impressions=impressions,
                    views=views,
                    likes=likes,
                    collects=int(likes * rng.uniform(0.3, 1.2)),
                    comments=int(likes * rng.uniform(0.05, 0.3)),
                    shares=int(likes * rng.uniform(0.02, 0.15)),
                    new_followers=int(likes * rng.uniform(0.03, 0.2)),
                )
            )
        return notes

    async def get_note(self, note_ref: str) -> NoteSnapshot:
        # Deterministic per note id, so analysing the same link gives the same numbers.
        seed = int(hashlib.sha256(note_ref.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        impressions = rng.randint(800, 30000)
        views = int(impressions * rng.uniform(0.06, 0.28))
        likes = int(views * rng.uniform(0.02, 0.12))
        return NoteSnapshot(
            xhs_note_id=note_ref,
            title=f"{rng.choice(_TITLES).format('小红书运营')}（{note_ref[:6]}）",
            note_type="video" if rng.random() < 0.3 else "image",
            tags=rng.sample(_TAGS, 3),
            published_at=datetime.now() - timedelta(days=rng.randint(1, 30)),
            impressions=impressions,
            views=views,
            likes=likes,
            collects=int(likes * rng.uniform(0.3, 1.2)),
            comments=int(likes * rng.uniform(0.05, 0.3)),
            shares=int(likes * rng.uniform(0.02, 0.15)),
            new_followers=int(likes * rng.uniform(0.03, 0.2)),
        )

    async def like(self, target_note: str) -> None:
        self._check(target_note)

    async def collect(self, target_note: str) -> None:
        self._check(target_note)

    async def comment(self, target_note: str, text: str) -> None:
        self._check(target_note)
        if not text.strip():
            raise ConnectorError("评论内容为空")

    @staticmethod
    def _check(target_note: str) -> None:
        if "fail" in target_note:  # lets tests / demos exercise the failure path
            raise ConnectorError("模拟失败：目标笔记不可用")


register_connector("mock", MockConnector)
