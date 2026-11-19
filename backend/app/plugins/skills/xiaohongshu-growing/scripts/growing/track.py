"""赛道判断：用采集到的真实数据给四大赛道打分，输出可解释的结构化结论。

打分不追求"精确预测"，而是把主观判断拆成可复核的 4 个客观分 + 1 个主观分，
每个分都带锚点和证据，避免"感觉这个赛道好"式的空话。
"""

from __future__ import annotations

from itertools import pairwise
from statistics import median

from . import hotspot, store
from .config import DEFAULT_TRACK_WEIGHTS, LANES, VIRAL_FLOOR, Lane, resolve_lane
from .metrics import summarize_notes, title_homogeneity
from .xhs import as_metrics


def _piecewise(value: float, anchors: list[tuple[float, float]]) -> float:
    """分段线性映射，anchor 需按 x 升序。"""
    if value <= anchors[0][0]:
        return anchors[0][1]
    if value >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in pairwise(anchors):
        if x0 <= value <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (value - x0) / (x1 - x0)
    return anchors[-1][1]


DEMAND_ANCHORS = [(0, 0), (200, 20), (500, 40), (1500, 60), (5000, 80), (15000, 95)]
HHI_ANCHORS = [(0.03, 0), (0.10, 20), (0.20, 45), (0.33, 70), (0.50, 90), (0.70, 100)]
TOP_SHARE_ANCHORS = [(0.05, 0), (0.15, 30), (0.25, 60), (0.40, 85), (0.60, 100)]


def lane_notes(conn, lane: Lane) -> list[dict]:
    """取该赛道已采集的笔记（按关键词归属）。"""
    rows = store.list_notes(conn, lane=lane.key, limit=500)
    keywords = set(lane.keywords)
    filtered = [r for r in rows if not r.get("keyword") or r.get("keyword") in keywords]
    return filtered or rows


def score_lane(
    conn,
    lane_name: str,
    fit: float = 60.0,
    weights: dict[str, float] | None = None,
) -> dict:
    """计算单个赛道的分项得分与综合分。"""
    lane = resolve_lane(lane_name)
    notes = lane_notes(conn, lane)
    metrics = as_metrics(notes)
    stats = summarize_notes(metrics, viral_floor=VIRAL_FLOOR.get(lane.key, 500))
    external = hotspot.external_heat(conn, lane.key)

    if stats["count"] == 0:
        return {
            "lane": lane.key,
            "lane_name": lane.name,
            "lane_is_custom": lane.is_custom,
            "score": 0.0,
            "data_status": "empty",
            "message": f"赛道「{lane.key}」还没有采集到样本，先执行 collect lane 再打分",
            "dimensions": {},
            "evidence": stats,
            "external_heat": external,
            "confidence": "none",
            "sample_size": 0,
            "pillars": list(lane.pillars),
            "audience": lane.audience,
            "barrier": lane.barrier,
            "asset_logic": lane.asset_logic,
            "monetize": list(lane.monetize),
            "top_notes": [],
            "gaps": ["无样本：先采集数据再判断"],
        }

    demand = _piecewise(stats["median_engage"], DEMAND_ANCHORS)
    saturation = 100 * (
        0.45 * _piecewise(stats["author_hhi"], HHI_ANCHORS) / 100
        + 0.30 * _piecewise(stats["top_author_share"], TOP_SHARE_ANCHORS) / 100
        + 0.25 * stats["title_homogeneity"]
    )
    breakout = 100 * (
        0.5 * (1 - min(1.0, stats["top_author_share"] / 0.5)) + 0.5 * stats["fresh_ratio"]
    )
    # 有作者粉丝数据时，用"小号占比"校正突围空间
    small_account_share = _small_account_share(conn, lane.key)
    if small_account_share is not None:
        breakout = 100 * (0.6 * small_account_share + 0.4 * stats["fresh_ratio"])

    commercial = lane.commercial_weight
    fits = max(0.0, min(100.0, fit))

    w = {**DEFAULT_TRACK_WEIGHTS, **(weights or {})}
    weight_sum = sum(w.values()) or 1.0
    # 外部热度默认权重 0：不进总分。用户显式打开但没数据时，把权重退回分母，
    # 不用 0 分去惩罚"没采数据"，避免把"没查"算成"不热"。
    use_external = (w.get("external") or 0) > 0 and external.get("available")
    if (w.get("external") or 0) > 0 and not use_external:
        weight_sum -= w["external"]
    total = (
        demand * w["demand"]
        + (100 - saturation) * w["saturation"]
        + breakout * w["breakout"]
        + commercial * w["commercial"]
        + fits * w["fit"]
    )
    if use_external:
        total += external["score"] * w["external"]
    total = total / (weight_sum or 1.0)

    confidence = "high" if stats["count"] >= 40 else "medium" if stats["count"] >= 20 else "low"
    return {
        "lane": lane.key,
        "lane_name": lane.name,
        "lane_is_custom": lane.is_custom,
        "score": round(total, 1),
        "data_status": "ok",
        "dimensions": {
            "demand": round(demand, 1),
            "saturation": round(min(100, saturation), 1),
            "breakout": round(breakout, 1),
            "commercial": round(commercial, 1),
            "fit": round(fits, 1),
        },
        "weights": w,
        "external_in_total": use_external,
        "external_heat": external,
        "evidence": stats,
        "small_account_share": small_account_share,
        "confidence": confidence,
        "sample_size": stats["count"],
        "pillars": list(lane.pillars),
        "audience": lane.audience,
        "barrier": lane.barrier,
        "asset_logic": lane.asset_logic,
        "monetize": list(lane.monetize),
        "top_notes": [
            {
                "feed_id": n["feed_id"],
                "title": n["title"],
                "author": n["author_name"],
                "liked": n["liked"],
                "collected": n["collected"],
                "comment": n["comment"],
                "note_type": n["note_type"],
            }
            for n in sorted(notes, key=lambda x: x.get("liked", 0), reverse=True)[:8]
        ],
        "gaps": _gaps(notes, stats),
    }


def _small_account_share(conn, lane: str) -> float | None:
    """已采到作者粉丝数时，算粉丝 <1 万的作者在热点作者里的占比。"""
    authors = [a for a in store.list_authors(conn, lane=lane) if a.get("fans")]
    if len(authors) < 3:
        return None
    small = [a for a in authors if a["fans"] < 10_000]
    return round(len(small) / len(authors), 3)


def _gaps(notes: list[dict], stats: dict) -> list[str]:
    """从数据里读出"供给缺口"，每条都要能对上数据。"""
    gaps: list[str] = []
    if stats["count"] == 0:
        return ["无样本：先采集数据再判断"]
    if stats["median_collect_ratio"] >= 0.5:
        gaps.append(
            f"收藏/点赞中位数 {stats['median_collect_ratio']}，用户把内容当资料存："
            "清单、攻略、可执行步骤类内容有长尾搜索价值"
        )
    if stats["median_comment_ratio"] >= 0.08:
        gaps.append(
            f"评论/点赞中位数 {stats['median_comment_ratio']}，讨论欲强："
            "带立场、带提问的选题容易起评论"
        )
    if stats["title_homogeneity"] >= 0.35:
        gaps.append(
            f"标题同质化 {stats['title_homogeneity']}，套路词高度重合："
            "换叙事视角（第一人称体验/失败复盘）比换关键词更容易差异化"
        )
    elif stats["title_homogeneity"] > 0:
        gaps.append(f"标题同质化仅 {stats['title_homogeneity']}，标题空间还比较宽松")
    if stats["video_ratio"] >= 0.6:
        gaps.append(f"视频占比 {stats['video_ratio']}，图文供给偏少：图文切入竞争更小")
    elif 0 < stats["video_ratio"] <= 0.2:
        gaps.append(f"视频占比仅 {stats['video_ratio']}，视频切入可能拿到额外分发")
    if stats["viral_rate"] <= 0.12:
        gaps.append(
            f"爆款率 {stats['viral_rate']}（门槛 {stats['viral_threshold']} 赞），"
            "流量集中：需要更强的钩子或更垂直的人群切口"
        )
    return gaps


def compare_lanes(
    fits: dict[str, float] | None = None,
    weights: dict[str, float] | None = None,
    lanes: list[str] | None = None,
) -> dict:
    """对多个赛道打分并排序。fits 是 Agent 依据用户输入给出的匹配度。"""
    targets = lanes or list(LANES)
    fits = fits or {}
    results = []
    with store.connect() as conn:
        for name in targets:
            lane = resolve_lane(name)
            fit = fits.get(lane.key, 60.0)
            results.append(score_lane(conn, lane.key, fit=fit, weights=weights))
    results.sort(key=lambda x: x["score"], reverse=True)
    return {
        "ranking": results,
        "recommendation": _recommend(results),
        "caveats": _caveats(results),
    }


def _recommend(results: list[dict]) -> dict:
    usable = [r for r in results if r.get("data_status") == "ok"]
    if not usable:
        return {
            "message": "所有赛道都没有采样数据，先对 4 个赛道各采集一次再比较",
            "next": "collect lane --lane 母婴育儿（其余赛道同理）",
        }
    results = usable
    if not results:
        return {}
    top = results[0]
    second = results[1] if len(results) > 1 else None
    reasons = []
    dims = top["dimensions"]
    if dims["demand"] >= 60:
        reasons.append(f"需求端有量：中位互动 {top['evidence']['median_engage']}")
    if dims["breakout"] >= 55:
        reasons.append("素人仍有突围空间：头部没有完全垄断，新内容能分到流量")
    if dims["saturation"] <= 50:
        reasons.append(f"供给饱和度 {dims['saturation']}，未被头部账号吃干")
    if dims["commercial"] >= 85:
        reasons.append(f"商业价值高（{top['lane_name']} 品牌投放密集）")
    if top["confidence"] == "low":
        reasons.append(f"注意：样本仅 {top['sample_size']} 条，结论置信度低，建议扩大采集")
    combo = ""
    if second and second["score"] >= top["score"] - 8:
        combo = (
            f"{top['lane_name']}为主 + {second['lane_name']}为辅："
            "主赛道立人设，副赛道做交叉选题（例如同一场景下的两种需求），"
            "能明显提高单篇的搜索命中面"
        )
    return {
        "primary": top["lane_name"],
        "primary_score": top["score"],
        "reasons": reasons,
        "secondary": second["lane_name"] if second else "",
        "combo_suggestion": combo,
    }


def _caveats(results: list[dict]) -> list[str]:
    caveats = []
    empty = [r["lane_name"] for r in results if r.get("data_status") == "empty"]
    if empty:
        caveats.append(f"{'、'.join(empty)} 尚未采集，未参与排序")
    low = [r["lane_name"] for r in results if r["confidence"] == "low"]
    if low:
        caveats.append(f"{'、'.join(low)} 样本不足 20 条，分数仅供参考，需补采")
    scored = [r for r in results if r.get("data_status") == "ok"]
    if scored and all(r["dimensions"]["fit"] == 60 for r in scored):
        caveats.append(
            "未提供个人匹配度（fit），默认按 60 中性计入，"
            "建议补充资源与经验信息重新打分"
        )
    caveats.append(
        "数据来源为当前登录账号可见的搜索结果，会受账号标签与推荐影响，"
        "跨账号/跨时间结果可能不同，建议每 2-4 周复采一次"
    )
    return caveats


def keyword_heat(conn, lane_name: str) -> list[dict]:
    """按关键词统计热度，用于找到赛道内的具体切口。"""
    lane = resolve_lane(lane_name)
    notes = lane_notes(conn, lane)
    buckets: dict[str, list[dict]] = {}
    for n in notes:
        buckets.setdefault(n.get("keyword") or "未知", []).append(n)
    out = []
    for keyword, items in buckets.items():
        likes = [i.get("liked", 0) for i in items]
        collects = [i.get("collected", 0) for i in items]
        out.append(
            {
                "keyword": keyword,
                "count": len(items),
                "median_liked": int(median(likes)) if likes else 0,
                "p90_liked": int(sorted(likes)[int(len(likes) * 0.9)] if likes else 0),
                "median_collected": int(median(collects)) if collects else 0,
                "title_homogeneity": title_homogeneity([i.get("title", "") for i in items]),
                "author_count": len({i.get("author_id") or i.get("author_name") for i in items}),
            }
        )
    out.sort(key=lambda x: x["median_liked"], reverse=True)
    return out
