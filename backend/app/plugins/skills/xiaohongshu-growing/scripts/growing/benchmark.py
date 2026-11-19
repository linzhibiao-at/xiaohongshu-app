"""对标拆解：把一个账号拆成"可复制的结构"和"不可复制的条件"。

核心原则：对标不是抄标题，而是回答两个问题
1. 这个账号的流量主要来自哪类内容（内容支柱 × 形式 × 钩子）
2. 这些内容里，哪些是我能复制的结构，哪些依赖我暂时没有的资源
"""

from __future__ import annotations

import json
from collections import Counter
from itertools import pairwise
from statistics import median

from . import store
from .config import LANES, VIRAL_FLOOR, resolve_lane
from .metrics import (
    detect_hook_patterns,
    percentile,
    summarize_notes,
    title_homogeneity,
)
from .xhs import as_metrics

# 内容支柱的识别线索：用标题/话题里的特征词把笔记归到支柱上
PILLAR_HINTS: dict[str, dict[str, tuple[str, ...]]] = {
    "母婴育儿": {
        "喂养辅食": ("辅食", "奶粉", "喂", "断奶", "食谱", "米糊", "餐", "吃得", "营养"),
        "成长发育": ("月龄", "发育", "翻身", "爬", "说话", "走路", "身高", "体重", "疫苗"),
        "亲子陪伴": ("亲子", "陪玩", "游戏", "户外", "绘本共读", "互动", "日常"),
        "用品避坑": ("好物", "避坑", "踩雷", "推荐", "测评", "清单", "必买", "鸡肋"),
        "绘本共读": ("绘本", "阅读", "书单", "讲故事", "识字"),
        "妈妈自我照顾": ("产后", "修复", "妈妈", "自己", "情绪", "睡眠", "身材"),
    },
    "美食探店": {
        "单店测评": ("探店", "这家", "店", "测评", "打卡", "新店"),
        "区域清单": ("合集", "清单", "条街", "商圈", "附近", "一条街", "几家"),
        "价格带对比": ("人均", "块", "元", "平价", "便宜", "性价比", "贵"),
        "隐藏吃法": ("吃法", "隐藏", "秘制", "搭配", "点单", "怎么点"),
        "排队避坑": ("排队", "等位", "避坑", "踩雷", "别去", "劝退", "值不值"),
        "本地时令": ("春天", "夏天", "秋冬", "时令", "限定", "季节", "新菜"),
    },
    "旅行出行": {
        "目的地攻略": ("攻略", "必去", "打卡", "景点", "路线", "地图"),
        "路线规划": ("路线", "几天", "行程", "day", "安排", "顺序"),
        "住宿选择": ("民宿", "酒店", "住宿", "住哪", "房型", "青旅"),
        "交通/预算": ("交通", "机票", "高铁", "自驾", "预算", "花", "人均"),
        "拍照机位": ("机位", "拍照", "出片", "构图", "最佳"),
        "季节限定": ("樱花", "秋", "冬天", "夏季", "花海", "雪", "季节"),
    },
    "家居生活": {
        "空间改造": ("改造", "装修", "翻新", "布置", "出租屋", "租房", "窗", "阳台"),
        "好物清单": ("好物", "清单", "推荐", "必买", "神器", "入手", "买了"),
        "收纳方法": ("收纳", "整理", "分类", "柜", "抽屉", "叠"),
        "装修避坑": ("避坑", "踩坑", "后悔", "别做", "经验", "工期"),
        "清洁维护": ("清洁", "打扫", "除味", "去污", "洗", "维护"),
        "氛围布置": ("氛围", "灯光", "软装", "香薰", "插花", "挂画", "配色"),
    },
}


def _classify_pillar(lane_key: str, title: str, tags: list[str]) -> str:
    hints = PILLAR_HINTS.get(lane_key, {})
    text = (title or "") + " " + " ".join(tags or [])
    best = "未归类"
    best_hits = 0
    for pillar, words in hints.items():
        hits = sum(1 for w in words if w in text)
        if hits > best_hits:
            best, best_hits = pillar, hits
    return best


def _fans_tier(fans: int) -> str:
    if fans <= 0:
        return "未知"
    if fans < 10_000:
        return "0-1万"
    if fans < 50_000:
        return "1-5万"
    if fans < 200_000:
        return "5-20万"
    return "20万+"


def _cadence(notes: list[dict]) -> dict:
    """用发布时间粗略估算更新节奏。样本不足时明确返回未知。"""
    stamps = sorted(n.get("publish_ts", 0) for n in notes if n.get("publish_ts"))
    if len(stamps) < 3:
        return {"updates_per_month": None, "median_gap_days": None, "note": "发布时间样本不足"}
    gaps = [(b - a) / 1000 / 86400 for a, b in pairwise(stamps)]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return {"updates_per_month": None, "median_gap_days": None, "note": "发布时间样本不足"}
    gap = median(gaps)
    return {
        "updates_per_month": round(30 / gap, 1) if gap else None,
        "median_gap_days": round(gap, 1),
        "note": "基于已采集笔记的发布时间差估算",
    }


def analyze_author(user_id: str, lane_name: str = "") -> dict:
    """拆解单个账号。lane 为空时从作者记录推断。"""
    with store.connect() as conn:
        author = store.get_author(conn, user_id) or {"user_id": user_id, "nickname": ""}
        lane_key = lane_name or author.get("lane", "")
        rows = conn.execute(
            "SELECT * FROM notes WHERE author_id=? ORDER BY publish_ts DESC", (user_id,)
        ).fetchall()
        notes = []
        for r in rows:
            item = dict(r)
            try:
                item["tags"] = json.loads(item.get("tags") or "[]")
            except json.JSONDecodeError:
                item["tags"] = []
            notes.append(item)
    if not notes:
        raise ValueError(f"账号 {user_id} 没有已采集的笔记，先执行 collect author")

    lane_key = lane_key or _guess_lane(notes)
    lane = resolve_lane(lane_key) if lane_key else None
    floor = VIRAL_FLOOR.get(lane.key, 500) if lane else 500
    metrics = as_metrics(notes)
    stats = summarize_notes(metrics, viral_floor=floor)
    threshold = max(floor, int(percentile([m.liked for m in metrics], 0.75)))
    viral = [n for n in notes if n.get("liked", 0) >= threshold]

    pillar_rows = []
    if lane:
        buckets: dict[str, list[dict]] = {}
        for n in notes:
            pillar = _classify_pillar(lane.key, n.get("title", ""), n.get("tags", []))
            buckets.setdefault(pillar, []).append(n)
        for pillar, items in buckets.items():
            likes = [i.get("liked", 0) for i in items]
            pillar_rows.append(
                {
                    "pillar": pillar,
                    "count": len(items),
                    "share": round(len(items) / len(notes), 3),
                    "median_liked": int(median(likes)) if likes else 0,
                    "max_liked": max(likes) if likes else 0,
                }
            )
        pillar_rows.sort(key=lambda x: x["median_liked"], reverse=True)

    tags = Counter(t for n in notes for t in (n.get("tags") or []))
    return {
        "author": {
            "user_id": user_id,
            "nickname": author.get("nickname", ""),
            "fans": author.get("fans", 0),
            "fans_raw": author.get("fans_raw", ""),
            "tier": _fans_tier(int(author.get("fans", 0))),
            "desc": author.get("desc", ""),
            "ip_location": author.get("ip_location", ""),
        },
        "lane": lane_key or "未识别",
        "summary": {
            **stats,
            "fan_efficiency": round(stats["median_liked"] / max(1, int(author.get("fans", 1))), 4),
            "viral_threshold_used": threshold,
        },
        "pillar_matrix": pillar_rows,
        "type_mix": dict(Counter(n.get("note_type", "") or "未知" for n in notes)),
        "title_hooks": detect_hook_patterns([n.get("title", "") for n in notes]),
        "title_homogeneity": title_homogeneity([n.get("title", "") for n in notes]),
        "tag_usage": [{"tag": t, "count": c} for t, c in tags.most_common(12)],
        "cadence": _cadence(notes),
        "top_notes": [
            {
                "feed_id": n["feed_id"],
                "title": n.get("title", ""),
                "liked": n.get("liked", 0),
                "collected": n.get("collected", 0),
                "comment": n.get("comment", 0),
                "note_type": n.get("note_type", ""),
                "publish_ts": n.get("publish_ts", 0),
                "tags": n.get("tags", []),
            }
            for n in sorted(notes, key=lambda x: x.get("liked", 0), reverse=True)[:10]
        ],
        "viral_notes": [
            {"feed_id": n["feed_id"], "title": n.get("title", ""), "liked": n.get("liked", 0)}
            for n in viral[:10]
        ],
        "copyable": _copyable(pillar_rows, notes, threshold),
        "not_copyable": _not_copyable(author, stats),
    }


def _guess_lane(notes: list[dict]) -> str:
    """按关键词命中推断账号所属赛道。"""
    text = " ".join((n.get("title") or "") + " " + " ".join(n.get("tags") or []) for n in notes)
    best, best_hits = "", 0
    for key, lane in LANES.items():
        hits = sum(1 for w in lane.keywords if w in text)
        if hits > best_hits:
            best, best_hits = key, hits
    return best


def _copyable(pillar_rows: list[dict], notes: list[dict], threshold: int) -> list[dict]:
    """能抄的是结构与选题，不是具体的图和经历。"""
    out: list[dict] = []
    for row in pillar_rows[:3]:
        if row["pillar"] == "未归类" or row["count"] < 2:
            continue
        out.append(
            {
                "type": "内容支柱",
                "target": row["pillar"],
                "evidence": (
                    f"{row['count']} 篇，中位 {row['median_liked']} 赞，"
                    f"占 {row['share']:.0%}"
                ),
                "how": "用同一支柱做出 3 篇系列，保持结构一致但换具体场景",
            }
        )
    hooks = detect_hook_patterns([n.get("title", "") for n in notes])[:3]
    for hook in hooks:
        example = hook["examples"][0] if hook["examples"] else ""
        out.append(
            {
                "type": "标题钩子",
                "target": hook["pattern"],
                "evidence": f"{hook['hits']} 篇命中，示例：{example}",
                "how": "沿用钩子类型，替换成自己赛道的具体对象与数字",
            }
        )
    viral_note = next((n for n in notes if n.get("liked", 0) >= threshold), None)
    if viral_note:
        out.append(
            {
                "type": "选题",
                "target": viral_note.get("title", ""),
                "evidence": (
                    f"{viral_note.get('liked', 0)} 赞 / "
                    f"{viral_note.get('collected', 0)} 收藏"
                ),
                "how": "同题目换人群或换城市重做一遍，竞争成本最低",
            }
        )
    return out


def _not_copyable(author: dict, stats: dict) -> list[dict]:
    out: list[dict] = []
    fans = int(author.get("fans", 0))
    if fans >= 50_000:
        out.append(
            {
                "type": "账号权重",
                "reason": f"对方粉丝 {fans}，同类内容在冷启动账号上的基础流量会被压制",
                "strategy": "不追同题大爆款，改做更垂直的小切口，先拿到搜索流量",
            }
        )
    if stats["top_author_share"] >= 0.2:
        out.append(
            {
                "type": "作者集中度",
                "reason": (
                    f"该作者自身内容集中度高（{stats['top_author_share']}），"
                    "靠个人 IP 而非选题"
                ),
                "strategy": "先抄选题结构，人设部分用自己的真实经历替换",
            }
        )
    if stats["video_ratio"] >= 0.6:
        out.append(
            {
                "type": "内容形式",
                "reason": "以视频为主，复制需要出镜与剪辑成本",
                "strategy": "先做同主题图文，验证选题后再考虑视频化",
            }
        )
    if not out:
        out.append(
            {
                "type": "无显著壁垒",
                "reason": "该账号暂未体现强资源壁垒",
                "strategy": "可优先作为 0-1 阶段对标对象",
            }
        )
    return out


def pick_benchmarks(lane_name: str, per_tier: int = 1) -> dict:
    """按粉丝分层挑对标账号：每层选互动最高的，覆盖"我一年内能到的位置"。"""
    lane = resolve_lane(lane_name)
    with store.connect() as conn:
        authors = store.list_authors(conn, lane=lane.key)
        notes = store.list_notes(conn, lane=lane.key, limit=500)
    by_author: dict[str, list[dict]] = {}
    for n in notes:
        by_author.setdefault(n.get("author_id", ""), []).append(n)

    tiers: dict[str, list[dict]] = {}
    for author in authors:
        if author.get("is_self"):
            continue
        uid = author["user_id"]
        items = by_author.get(uid, [])
        if not items:
            continue
        likes = [i.get("liked", 0) for i in items]
        tier = _fans_tier(int(author.get("fans", 0)))
        tiers.setdefault(tier, []).append(
            {
                "user_id": uid,
                "nickname": author.get("nickname", ""),
                "fans": author.get("fans", 0),
                "note_count": len(items),
                "median_liked": int(median(likes)) if likes else 0,
                "max_liked": max(likes) if likes else 0,
                "tier": tier,
            }
        )
    picked: list[dict] = []
    for tier in ("0-1万", "1-5万", "5-20万", "20万+", "未知"):
        items = sorted(tiers.get(tier, []), key=lambda x: x["median_liked"], reverse=True)
        picked.extend(items[:per_tier])
    return {
        "lane": lane.key,
        "benchmarks": picked,
        "tiers_available": {k: len(v) for k, v in tiers.items()},
        "note": "优先看 0-1 万与 1-5 万两层：那才是 3-12 个月内可复制的水平",
        "next_step": "对选中的账号执行 benchmark author --user-id <id> 做逐条拆解",
    }


def gap_against_self(benchmark_user_id: str, lane_name: str = "") -> dict:
    """自己 vs 对标：只看差距结构，不做无意义的绝对值比较。"""
    bench = analyze_author(benchmark_user_id, lane_name)
    with store.connect() as conn:
        self_id = store.kv_get(conn, "self_user_id", "")
        self_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM notes WHERE author_id=?", (self_id,)
        ).fetchall()] if self_id else []
        self_author = store.get_author(conn, self_id) if self_id else None
    if not self_rows:
        return {
            "benchmark": bench["author"],
            "self": None,
            "message": "尚未采集自己账号的笔记，先执行 profile-set --url <主页链接>",
        }
    self_stats = summarize_notes(
        as_metrics(self_rows),
        viral_floor=VIRAL_FLOOR.get(resolve_lane(lane_name).key if lane_name else "家居生活", 500),
    )
    actions: list[str] = []
    b = bench["summary"]
    if self_stats["median_liked"] < b["median_liked"] * 0.5:
        actions.append("单篇流量差距最大：先修封面与标题钩子，再谈选题")
    if self_stats["count"] < (bench["cadence"]["updates_per_month"] or 0) * 2:
        actions.append("更新量不足：对方月更节奏明显更高，先把周更稳定在 3-4 篇")
    if (bench["summary"]["viral_rate"] or 0) > (self_stats["viral_rate"] or 0) * 1.5:
        actions.append("爆款率差距大：把对方已验证的选题结构重做一遍，比自己原创更省力")
    return {
        "benchmark": bench["author"],
        "self": {
            "user_id": self_id,
            "nickname": (self_author or {}).get("nickname", ""),
            "fans": (self_author or {}).get("fans", 0),
            "note_count": len(self_rows),
            "summary": self_stats,
        },
        "benchmark_summary": b,
        "actions": actions or ["各项差距都不算大，重点转向选题密度与系列化"],
    }
