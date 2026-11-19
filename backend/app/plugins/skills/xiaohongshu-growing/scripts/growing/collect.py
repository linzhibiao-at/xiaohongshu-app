"""采集编排：把小红书公开数据抓进本地快照库。

每次采集都会记录"来源关键词 + 排序方式 + 采集时间"，报告里可以完整回溯。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field

from . import store, xhs
from .config import Lane, resolve_lane
from .metrics import parse_count

# 两次搜索之间的基础间隔（秒），防止高频搜索
SEARCH_GAP = (3.0, 6.0)


@dataclass
class CollectConfig:
    """一次赛道采集的规模控制。默认值在风控与样本量之间折中。"""

    keywords: list[str] = field(default_factory=list)
    per_keyword: int = 12
    detail_top: int = 6
    with_fresh: bool = True
    with_authors: bool = False
    author_top: int = 5
    load_comments: bool = False


def _log(msg: str) -> None:
    print(f"[growing] {msg}", file=sys.stderr)


def collect_search(
    conn,
    lane: Lane,
    keyword: str,
    sort_by: str = "最多点赞",
    publish_time: str = "",
    note_type: str = "",
    limit: int = 12,
) -> list[dict]:
    """搜索一个关键词并落库，返回归一化后的笔记列表。"""
    feeds = xhs.search(keyword, sort_by=sort_by, publish_time=publish_time, note_type=note_type)
    notes: list[dict] = []
    for feed in feeds[:limit]:
        note = xhs.normalize_feed(feed, lane=lane.key, keyword=keyword)
        if not note["feed_id"]:
            continue
        store.upsert_note(conn, note)
        notes.append(note)
    store.record_search(
        conn,
        {
            "keyword": keyword,
            "lane": lane.key,
            "sort_by": sort_by,
            "publish_time": publish_time,
            "note_type": note_type,
            "result_count": len(feeds),
            "note_ids": [n["feed_id"] for n in notes],
        },
    )
    _log(f"搜索「{keyword}」({sort_by or '综合'}) 命中 {len(feeds)} 条，入库 {len(notes)} 条")
    return notes


def collect_detail(conn, note: dict, keyword: str = "", load_comments: bool = False) -> dict:
    """抓一篇笔记详情，补全正文、话题、发布时间。"""
    detail = xhs.feed_detail(
        note["feed_id"], note.get("xsec_token", ""), load_comments=load_comments
    )
    normalized = xhs.normalize_detail(detail, lane=note.get("lane", ""), keyword=keyword)
    if not normalized["feed_id"]:
        normalized["feed_id"] = note["feed_id"]
    if not normalized["xsec_token"]:
        normalized["xsec_token"] = note.get("xsec_token", "")
    store.upsert_note(conn, normalized)
    store.record_snapshot(conn, normalized["feed_id"])
    normalized["_comments"] = xhs.comments_of(detail)
    return normalized


def collect_author(conn, user_id: str, xsec_token: str = "", lane: str = "") -> dict:
    """抓作者主页：粉丝数 + 笔记列表。"""
    profile = xhs.user_profile(user_id, xsec_token)
    author = xhs.normalize_author(profile, lane=lane)
    author["user_id"] = author.get("user_id") or user_id
    store.upsert_author(conn, author)
    feeds = profile.get("feeds") or []
    for feed in feeds[:30]:
        note = xhs.normalize_feed(feed, lane=lane)
        if note["feed_id"]:
            note["author_id"] = note["author_id"] or author["user_id"]
            store.upsert_note(conn, note)
    _log(f"作者「{author['nickname'] or user_id}」粉丝 {author['fans']}，笔记 {len(feeds)} 篇入库")
    return author


def collect_lane(lane_name: str, cfg: CollectConfig | None = None) -> dict:
    """按赛道批量采集：多关键词搜索 → 高互动笔记详情 → 可选作者主页。"""
    lane = resolve_lane(lane_name)
    cfg = cfg or CollectConfig()
    keywords = cfg.keywords or list(lane.keywords[:4])
    stats = {
        "lane": lane.key,
        "keywords": keywords,
        "searched": 0,
        "notes": 0,
        "details": 0,
        "authors": 0,
        "failed": [],
    }
    with store.connect() as conn:
        pool: dict[str, dict] = {}
        for keyword in keywords:
            try:
                notes = collect_search(
                    conn, lane, keyword, sort_by="最多点赞", limit=cfg.per_keyword
                )
                stats["searched"] += 1
                stats["notes"] += len(notes)
                for n in notes:
                    pool.setdefault(n["feed_id"], n)
                time.sleep(_gap())
                if cfg.with_fresh:
                    fresh = collect_search(
                        conn,
                        lane,
                        keyword,
                        sort_by="最新",
                        publish_time="一周内",
                        limit=cfg.per_keyword,
                    )
                    stats["notes"] += len(fresh)
                    for n in fresh:
                        pool.setdefault(n["feed_id"], n)
                    time.sleep(_gap())
            except xhs.XhsError as exc:
                stats["failed"].append({"keyword": keyword, "error": str(exc)})
                _log(f"搜索「{keyword}」失败：{exc}")

        # 抽样详情：优先抓互动最高的笔记，用于拿发布时间与正文结构
        ranked = sorted(pool.values(), key=lambda n: n.get("liked", 0), reverse=True)
        for note in ranked[: cfg.detail_top]:
            try:
                collect_detail(conn, note, keyword=note.get("keyword", ""), load_comments=False)
                stats["details"] += 1
            except xhs.XhsError as exc:
                stats["failed"].append({"feed_id": note["feed_id"], "error": str(exc)})

        # 可选：抓头部作者主页，拿到粉丝数才能判断"素人能不能突围"
        if cfg.with_authors:
            seen: set[str] = set()
            for note in ranked:
                uid = note.get("author_id", "")
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                if len(seen) > cfg.author_top:
                    break
                try:
                    collect_author(conn, uid, lane=lane.key)
                    stats["authors"] += 1
                    time.sleep(_gap())
                except xhs.XhsError as exc:
                    stats["failed"].append({"author": uid, "error": str(exc)})
    return stats


def collect_keyword_freshness(lane_name: str, keyword: str) -> dict:
    """对比"最多点赞"与"一周内最新"两组结果，低成本估算内容新鲜度与新人天花板。"""
    lane = resolve_lane(lane_name)
    with store.connect() as conn:
        hot = collect_search(conn, lane, keyword, sort_by="最多点赞", limit=20)
        newest = collect_search(
            conn, lane, keyword, sort_by="最新", publish_time="一周内", limit=20
        )
    return {"hot": hot, "newest": newest}


def collect_self_profile(url: str) -> dict:
    """登记自己的账号，采集已有笔记用于体检与复盘。"""
    user_id, token = xhs.parse_own_profile_url(url)
    with store.connect() as conn:
        author = collect_author(conn, user_id, token)
        conn.execute("UPDATE authors SET is_self=1 WHERE user_id=?", (user_id,))
        conn.execute("UPDATE notes SET is_self=1 WHERE author_id=?", (user_id,))
        store.kv_set(conn, "self_user_id", user_id)
        store.kv_set(conn, "self_xsec_token", token)
    return {
        "user_id": user_id,
        "nickname": author.get("nickname", ""),
        "fans": author.get("fans", 0),
    }


def refresh_self_stats() -> dict:
    """重新拉取自己账号的互动数据，写入快照，用于复盘曲线。"""
    with store.connect() as conn:
        user_id = store.kv_get(conn, "self_user_id", "")
        token = store.kv_get(conn, "self_xsec_token", "")
        if not user_id:
            raise ValueError("尚未登记自己的账号，请先执行 profile-set --url <主页链接>")
        author = collect_author(conn, user_id, token)
        conn.execute("UPDATE notes SET is_self=1 WHERE author_id=?", (user_id,))
        rows = conn.execute("SELECT feed_id FROM notes WHERE author_id=?", (user_id,)).fetchall()
        for row in rows:
            store.record_snapshot(conn, row["feed_id"])
        return {
            "user_id": user_id,
            "fans": author.get("fans", 0),
            "snapshots": len(rows),
        }


def seed_lane_lookup(lane: str) -> Lane:
    """按赛道名取定义，四大赛道走预设，其余走自由赛道兜底。"""
    return resolve_lane(lane)


def _gap() -> float:
    import random

    return random.uniform(*SEARCH_GAP)


def count_of(raw: str) -> int:
    return parse_count(raw).value
