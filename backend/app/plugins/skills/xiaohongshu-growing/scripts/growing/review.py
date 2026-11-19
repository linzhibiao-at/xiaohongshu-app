"""数据复盘：把"感觉不行"拆成可验证的指标差距，并给出下一步动作。

复盘的对象只有三类：
1. 单篇：这篇为什么行/不行
2. 周/阶段：这个阶段验证出了什么
3. 账号：粉丝、更新节奏、内容支柱结构是否健康
"""

from __future__ import annotations

from statistics import median

from . import store
from .config import LANES, PUBLISH_SLOTS, VIRAL_FLOOR, resolve_lane
from .metrics import summarize_notes
from .xhs import as_metrics


def _lane_baseline(lane_key: str) -> dict:
    with store.connect() as conn:
        rows = store.list_notes(conn, lane=lane_key, limit=500)
    lane = resolve_lane(lane_key) if lane_key else None
    if lane:
        keywords = set(lane.keywords)
        filtered = [r for r in rows if not r.get("keyword") or r.get("keyword") in keywords]
        rows = filtered or rows
    if not rows:
        return {"count": 0}
    stats = summarize_notes(as_metrics(rows), viral_floor=VIRAL_FLOOR.get(lane_key, 500))
    return stats


def note_review(feed_id: str, lane_name: str = "") -> dict:
    """单篇复盘：和自己的赛道基准比，而不是和爆款比。"""
    with store.connect() as conn:
        note = store.get_note(conn, feed_id)
        if not note:
            raise ValueError(f"笔记 {feed_id} 不在本地库，先执行 collect detail")
        lane_key = lane_name or note.get("lane", "") or _guess_lane_from_note(note)
        hits = _is_self_note(conn, note)
        snapshots = [
            dict(r)
            for r in conn.execute(
                "SELECT captured_at, day_index, liked, collected, comment FROM note_snapshots"
                " WHERE feed_id=? ORDER BY day_index, captured_at",
                (feed_id,),
            ).fetchall()
        ]
    baseline = _lane_baseline(lane_key)
    if baseline.get("count", 0) == 0:
        return {
            "note": _brief(note),
            "lane": lane_key,
            "message": f"赛道「{lane_key}」没有基准数据，先执行 collect lane --lane {lane_key}",
            "is_self": hits,
        }

    likes = _percentile_rank(conn_notes(lane_key), note.get("liked", 0))
    collect_ratio = _ratio(note.get("collected", 0), note.get("liked", 1))
    comment_ratio = _ratio(note.get("comment", 0), note.get("liked", 1))
    diagnosis: list[str] = []
    actions: list[str] = []

    base_median = baseline["median_liked"]
    if note.get("liked", 0) >= baseline["viral_threshold"]:
        diagnosis.append(
            f"流量达标：{note.get('liked')} 赞，超过赛道爆款门槛 {baseline['viral_threshold']}"
        )
        actions.append("立刻做同题材系列第 2 篇，趁账号已被打上标签")
    elif note.get("liked", 0) < base_median * 0.5:
        diagnosis.append(
            f"流量未起：{note.get('liked')} 赞，不到赛道中位 {base_median} 的一半"
        )
        actions.append("先换封面与标题钩子重发同主题，不要急着换赛道")
    else:
        diagnosis.append(f"流量正常区间：{note.get('liked')} 赞（赛道中位 {base_median}）")

    if collect_ratio >= baseline.get("median_collect_ratio", 0) * 1.3:
        diagnosis.append(f"收藏/点赞 {collect_ratio}，实用价值被认可，具备搜索长尾潜力")
        actions.append("在正文补一版「按场景分组的清单」，把长尾搜索词铺开")
    elif collect_ratio < baseline.get("median_collect_ratio", 0) * 0.6:
        diagnosis.append(f"收藏/点赞 {collect_ratio}，低于赛道基准：内容偏「看过就好」")
        actions.append("补可执行步骤/价格/参数，让读者有「要存下来」的理由")

    if comment_ratio < baseline.get("median_comment_ratio", 0) * 0.6:
        diagnosis.append(f"评论/点赞 {comment_ratio}，讨论度不足")
        actions.append("结尾加一个二选一提问，比开放式提问更容易起评论")

    slot = _publish_slot(note.get("publish_ts", 0))
    if slot and slot not in PUBLISH_SLOTS.get(lane_key, ()):
        diagnosis.append(f"发布时间 {slot} 不在该赛道常用时段 {PUBLISH_SLOTS.get(lane_key)}")
        actions.append(f"下一批改到 {PUBLISH_SLOTS.get(lane_key, ('12:00',))[0]} 附近发布做对照")

    return {
        "note": _brief(note),
        "is_self": hits,
        "lane": lane_key,
        "percentile_in_lane": likes,
        "metrics": {
            "liked": note.get("liked", 0),
            "collected": note.get("collected", 0),
            "comment": note.get("comment", 0),
            "collect_ratio": collect_ratio,
            "comment_ratio": comment_ratio,
        },
        "baseline": {
            key: baseline.get(key)
            for key in (
                "median_liked",
                "viral_threshold",
                "median_collect_ratio",
                "median_comment_ratio",
                "count",
            )
        },
        "diagnosis": diagnosis,
        "actions": actions,
        "snapshots": snapshots,
        "growth": _growth(snapshots),
    }


def _growth(snapshots: list[dict]) -> dict:
    if len(snapshots) < 2:
        return {"available": False, "note": "只有 1 次快照，无法计算增长，需隔天再采一次"}
    first, last = snapshots[0], snapshots[-1]
    return {
        "available": True,
        "liked_delta": last["liked"] - first["liked"],
        "collected_delta": last["collected"] - first["collected"],
        "comment_delta": last["comment"] - first["comment"],
        "points": len(snapshots),
    }


def conn_notes(lane_key: str) -> list[int]:
    with store.connect() as conn:
        rows = store.list_notes(conn, lane=lane_key, limit=500)
    return [r.get("liked", 0) for r in rows]


def _percentile_rank(population: list[int], value: int) -> float:
    if not population:
        return 0.0
    below = len([v for v in population if v < value])
    return round(below / len(population), 3)


def _ratio(a: int, b: int) -> float:
    return round(a / b, 3) if b else 0.0


def _publish_slot(publish_ts: int) -> str:
    if not publish_ts:
        return ""
    from datetime import datetime

    return datetime.fromtimestamp(publish_ts / 1000).strftime("%H:%M")


def _brief(note: dict) -> dict:
    return {
        "feed_id": note.get("feed_id", ""),
        "title": note.get("title", ""),
        "note_type": note.get("note_type", ""),
        "author": note.get("author_name", ""),
        "tags": note.get("tags", []),
        "publish_ts": note.get("publish_ts", 0),
    }


def _guess_lane_from_note(note: dict) -> str:
    text = (note.get("title", "") or "") + " " + " ".join(note.get("tags") or [])
    best, hits = "", 0
    for key, lane in LANES.items():
        score = sum(1 for w in lane.keywords if w in text)
        if score > hits:
            best, hits = key, score
    return best or "家居生活"


def _is_self_note(conn, note: dict) -> bool:
    self_id = store.kv_get(conn, "self_user_id", "")
    return bool(self_id) and note.get("author_id") == self_id


def weekly_review(plan_id: int | None = None) -> dict:
    """阶段复盘：计划完成度 + 实际数据 + 支柱胜负。"""
    with store.connect() as conn:
        plan = store.get_plan(conn, plan_id) if plan_id else store.latest_plan(conn)
        self_id = store.kv_get(conn, "self_user_id", "")
        if not self_id:
            return {"message": "尚未登记自己的账号，先执行 profile-set --url <主页链接>"}
        rows = [
            dict(r)
            for r in conn.execute("SELECT * FROM notes WHERE is_self=1").fetchall()
        ]
        author = store.get_author(conn, self_id) or {}
    if not rows:
        return {"message": "还没有采集到自己已发布的笔记"}

    lane_key = (plan or {}).get("lane") or author.get("lane", "") or "家居生活"
    baseline = _lane_baseline(lane_key)
    stats = summarize_notes(as_metrics(rows), viral_floor=VIRAL_FLOOR.get(lane_key, 500))

    by_pillar: dict[str, list[int]] = {}
    from .benchmark import _classify_pillar

    for row in rows:
        pillar = _classify_pillar(lane_key, row.get("title", ""), [])
        by_pillar.setdefault(pillar, []).append(row.get("liked", 0))
    pillar_rank = sorted(
        (
            {"pillar": p, "count": len(v), "median_liked": int(median(v))}
            for p, v in by_pillar.items()
        ),
        key=lambda x: x["median_liked"],
        reverse=True,
    )

    completion = None
    if plan:
        items = plan["items"]
        done = [i for i in items if i["status"] in ("published", "done")]
        completion = {
            "plan_id": plan["id"],
            "planned": len(items),
            "published": len(done),
            "rate": round(len(done) / len(items), 3) if items else 0,
        }

    verdict: list[str] = []
    if baseline.get("count"):
        gap = stats["median_liked"] - baseline["median_liked"]
        verdict.append(
            f"账号单篇中位 {stats['median_liked']} 赞，赛道中位 {baseline['median_liked']} 赞，"
            f"差 {gap:+d}"
        )
    if stats["viral_count"]:
        verdict.append(f"近期出现 {stats['viral_count']} 篇爆款（门槛 {stats['viral_threshold']}）")
    else:
        verdict.append("近期没有爆款：优先改钩子与封面，不要同时改赛道")
    if completion and completion["rate"] < 0.8:
        verdict.append(
            f"排期完成率 {completion['rate']:.0%}：更新不稳定会直接影响账号权重"
        )
    if pillar_rank:
        verdict.append(
            f"表现最好的支柱是「{pillar_rank[0]['pillar']}」"
            f"（{pillar_rank[0]['count']} 篇，中位 {pillar_rank[0]['median_liked']} 赞）"
        )

    return {
        "account": {
            "user_id": self_id,
            "nickname": author.get("nickname", ""),
            "fans": author.get("fans", 0),
            "note_count": len(rows),
        },
        "stats": stats,
        "baseline": {
            "median_liked": baseline.get("median_liked"),
            "viral_threshold": baseline.get("viral_threshold"),
            "count": baseline.get("count", 0),
        },
        "completion": completion,
        "pillar_rank": pillar_rank,
        "verdict": verdict,
        "next_actions": _next_actions(stats, baseline, pillar_rank),
    }


def _next_actions(stats: dict, baseline: dict, pillar_rank: list[dict]) -> list[str]:
    actions = []
    if baseline.get("count") and stats["median_liked"] < baseline["median_liked"] * 0.6:
        actions.append("先复制赛道内已验证的选题结构，停止纯原创实验")
    if stats["count"] and stats["viral_rate"] < 0.1:
        actions.append("每 5 篇里至少留 1 篇追热点/追搜索词，拉曝光")
    if pillar_rank and pillar_rank[0]["count"] < 3:
        actions.append(f"「{pillar_rank[0]['pillar']}」样本太少，先做到 3 篇再判断")
    if not actions:
        actions.append("保持当前节奏，下一阶段放大已胜出的支柱并做系列化")
    return actions
