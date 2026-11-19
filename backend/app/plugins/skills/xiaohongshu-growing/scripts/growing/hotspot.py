"""跨平台热点层：收外部热点信号，判断"这个方向值不值得在小红书下注"。

职责边界：
- 只负责收信号与判断错位，不改赛道主分（主分见 track.py）。
- 三个数据入口，等级不同，能下的结论强度也不同：

  L1  pull_douyin()  抖音公开热榜直连接口，无中间层，只作外部热度信号
  L2  pull_xhs()    站内 CLI 真实采集，可直接引用
  L3  ingest()      公开网页/搜索结果，由 Agent 整理成 JSON 后入库，只能作方向提示

一条原则：抖音热不等于小红书能做。热点的价值在于"发现需求",
能不能接住取决于站内供给够不够少、内容形态能不能迁移。
"""

from __future__ import annotations

import json
import statistics
import urllib.request
from pathlib import Path
from urllib.parse import quote

from . import collect, store, xhs
from .config import (
    DOUYIN_HOT_TIMEOUT,
    DOUYIN_HOT_WORD_URL,
    HOTSPOT_PLATFORMS,
    HOTSPOT_SOURCE_LEVELS,
    HOTSPOT_SPILL_HEAT,
    HOTSPOT_SUPPLY_GAP,
    resolve_lane,
)
from .metrics import eng_score

# 访问平台公开接口时用的 UA，缺省会直接被挡
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _check_platform(platform: str) -> str:
    key = (platform or "").strip().lower()
    if key not in HOTSPOT_PLATFORMS:
        raise ValueError(
            f"未知平台：{platform}，当前支持：{'、'.join(HOTSPOT_PLATFORMS)}"
        )
    return key


# ─── L1：平台公开接口直连 ────────────────────────────────────────────────────


def _fetch_douyin_hot(timeout: int = DOUYIN_HOT_TIMEOUT) -> list[dict]:
    """请求抖音热榜公开接口，返回原始词条列表。"""
    req = urllib.request.Request(
        DOUYIN_HOT_WORD_URL,
        headers={"User-Agent": _UA, "Referer": "https://www.douyin.com/"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", "ignore"))

    out: list[dict] = []
    for item in payload.get("word_list") or []:
        word = str(item.get("word") or "").strip()
        if not word:
            continue
        out.append(
            {
                "word": word,
                "hot_value": _to_float(item.get("hot_value")),
                "label": item.get("label", 0),
            }
        )
    return out


def _lane_hits(word: str, lane) -> bool:
    """热榜词是否命中赛道。只做字面包含，不做语义推测。"""
    text = (word or "").lower()
    if not text:
        return False
    for term in (lane.name, *lane.keywords):
        term = (term or "").strip().lower()
        if term and term in text:
            return True
    return False


def pull_douyin(
    lane_name: str = "",
    limit: int = 50,
    timeout: int = DOUYIN_HOT_TIMEOUT,
) -> dict:
    """拉抖音全国热榜（L1），并把命中赛道的词绑定到赛道。

    全国热榜是大众人气榜，与细分赛道大多不重合。命中数 0 是常态，
    它只说明“这个方向今天不在大众热点上”，不能反推出“这个赛道不能做”。
    所以没命中的词也会存下来（lane 为空），作为全网热点池供人工扫一眼。
    """
    lane = resolve_lane(lane_name) if lane_name else None
    rows = _fetch_douyin_hot(timeout=timeout)[:limit]
    matched: list[dict] = []
    pool: list[dict] = []

    with store.connect() as conn:
        for idx, item in enumerate(rows, start=1):
            word = item["word"]
            hit = bool(lane and _lane_hits(word, lane))
            signal = {
                "platform": "douyin",
                "lane": lane.key if hit else "",
                "keyword": word,
                "raw_title": word,
                "heat_value": item["hot_value"],
                "heat_raw": f"抖音热榜热度 {int(item['hot_value'])}",
                "rank": idx,
                "source_level": "L1",
                "source_url": DOUYIN_HOT_WORD_URL,
                "meta": {"heat_basis": "hot_value", "label": item["label"]},
            }
            store.upsert_hotspot(conn, signal)
            (matched if hit else pool).append(signal)

    note = (
        f"抖音全国热榜 {len(rows)} 条，命中本赛道 {len(matched)} 条。"
        "命中的词才是大众层面的同题热点；没命中正常，不代表赛道不行。"
    )
    if lane and not matched:
        note += (
            "当前赛道不在今日大众热榜上，"
            "想看赛道内部的搜索需求请改用 hotspot hotwords --platform xhs。"
        )

    return {
        "lane": lane.key if lane else "",
        "platform": "douyin",
        "source_level": "L1",
        "source": DOUYIN_HOT_WORD_URL,
        "fetched": len(rows),
        "matched_count": len(matched),
        "matched": matched,
        "pool_top": pool[:20],
        "strength": _level_strength("L1"),
        "note": note,
    }


def pull_xhs_insights(
    lane_name: str,
    keywords: list[str] | None = None,
    per_keyword: int = 10,
) -> dict:
    """采小红书站内搜索需求侧信号（L1）。

    取两类信号：
      联想词   用户搜这个关键词时实际接下来搜什么，按真实搜索行为排序
      相关话题 站内已把内容归成哪些子题，反映选题怎么切

    联想词没有热度数值，只有位次，所以 heat_value 留 0、rank 填真实位次，
    热度口径记为 search_rank。话题只在返回里给出，不入库：
    它们的顺序是内容组织顺序，不是热度排序，混进趋势库会误导涨跌判断。
    """
    lane = resolve_lane(lane_name)
    targets = keywords or list(lane.keywords[:2])
    signals: list[dict] = []
    topics: list[dict] = []

    with store.connect() as conn:
        for seed in targets:
            data = xhs.hot_suggest(seed)
            suggestions = (data.get("suggestions") or [])[:per_keyword]
            for idx, item in enumerate(suggestions, start=1):
                text = item["text"]
                signal = {
                    "platform": "xhs",
                    "lane": lane.key,
                    "keyword": text,
                    "raw_title": text,
                    "heat_value": 0.0,
                    "heat_raw": f"搜索建议第 {idx} 位",
                    "rank": idx,
                    "source_level": "L1",
                    "source_url": (
                        "https://www.xiaohongshu.com/search_result?keyword="
                        + quote(text)
                    ),
                    "meta": {
                        "heat_basis": "search_rank",
                        "seed_keyword": seed,
                        "suggestion_type": item.get("type", ""),
                    },
                }
                store.upsert_hotspot(conn, signal)
                signals.append(signal)
            for tag in data.get("tags") or []:
                topics.append({"seed_keyword": seed, **tag})

    return {
        "lane": lane.key,
        "platform": "xhs",
        "source_level": "L1",
        "seeds": targets,
        "pulled": len(signals),
        "signals": signals,
        "topics": topics,
        "strength": _level_strength("L1", basis="search_rank"),
        "note": (
            "联想词是站内真实搜索需求，可直接用来拆选题；"
            "topics 是站内话题归类，只作选题灵感，顺序不代表热度。"
            "位次数字只在本组内可比较，不能跨关键词相加。"
        ),
    }


# ─── L2：站内真实采集 ─────────────────────────────────────────────────────────


def pull_xhs(
    lane_name: str,
    keywords: list[str] | None = None,
    per_keyword: int = 12,
    sort_by: str = "最多点赞",
) -> dict:
    """用站内 CLI 采集赛道关键词，把样本量与中位互动折算成站内热度信号。

    这一步会真实发起搜索（受风控节流约束），产出的信号等级为 L2。
    """
    lane = resolve_lane(lane_name)
    targets = keywords or list(lane.keywords[:3])
    signals: list[dict] = []
    with store.connect() as conn:
        for keyword in targets:
            notes = collect.collect_search(
                conn, lane, keyword, sort_by=sort_by, limit=per_keyword
            )
            if not notes:
                continue
            scores = [
                eng_score(
                    n.get("liked", 0),
                    n.get("collected", 0),
                    n.get("comment", 0),
                    n.get("shared", 0),
                )
                for n in notes
            ]
            median_eng = statistics.median(scores) if scores else 0.0
            top = max(notes, key=lambda x: x.get("liked", 0))
            signal = {
                "platform": "xhs",
                "lane": lane.key,
                "keyword": keyword,
                "raw_title": top.get("title", ""),
                "heat_value": round(float(median_eng), 1),
                "heat_raw": f"中位互动 {int(median_eng)}",
                "rank": 0,
                "source_level": "L2",
                "source_url": "",
                "meta": {
                    "sample_count": len(notes),
                    "sort_by": sort_by,
                    "max_liked": max(n.get("liked", 0) for n in notes),
                    "heat_basis": "engagement_median",
                },
            }
            store.upsert_hotspot(conn, signal)
            signals.append(signal)
    return {
        "lane": lane.key,
        "platform": "xhs",
        "source_level": "L2",
        "pulled": len(signals),
        "signals": signals,
        "note": "站内真实采集，可直接引用；样本 <20 条时中位互动波动大，需标低置信",
    }


# ─── L3：网页/搜索整理后入库 ───────────────────────────────────────────────────


def ingest(
    path: str,
    platform: str,
    lane_name: str = "",
    source_level: str = "L3",
    captured_at: str = "",
) -> dict:
    """把 Agent 整理好的热点 JSON 写入库。

    支持两种结构：
      1) [{"keyword": "...", "heat": 123, "rank": 3, "title": "...", "url": "..."}]
      2) {"items": [...], "captured_at": "...", "platform": "douyin"}

    keyword 字段名兼容 keyword / word / query；热度兼容 heat / hot / value。
    """
    key = _check_platform(platform)
    if source_level not in HOTSPOT_SOURCE_LEVELS:
        raise ValueError(
            f"未知来源等级：{source_level}，可选：{'、'.join(HOTSPOT_SOURCE_LEVELS)}"
        )
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        items = raw.get("items") or raw.get("list") or []
        captured_at = captured_at or str(raw.get("captured_at") or "")
    else:
        items = raw
    if not isinstance(items, list) or not items:
        raise ValueError("热点文件里没有可用的 items")

    lane_key = resolve_lane(lane_name).key if lane_name else ""
    accepted, skipped = 0, 0
    with store.connect() as conn:
        for item in items:
            if not isinstance(item, dict):
                skipped += 1
                continue
            keyword = str(
                item.get("keyword") or item.get("word") or item.get("query") or ""
            ).strip()
            if not keyword:
                skipped += 1
                continue
            heat = item.get("heat", item.get("hot", item.get("value", 0)))
            signal = {
                "platform": key,
                "lane": str(item.get("lane") or lane_key),
                "keyword": keyword,
                "raw_title": str(item.get("title") or item.get("raw_title") or ""),
                "heat_value": _to_float(heat),
                "heat_raw": str(item.get("heat_raw") or heat or ""),
                "rank": int(item.get("rank") or 0),
                "source_level": source_level,
                "source_url": str(item.get("url") or item.get("source_url") or ""),
                "meta": {
                    "ingested_from": Path(path).name,
                    "extra": item.get("meta") or {},
                },
            }
            if captured_at:
                signal["captured_at"] = captured_at
            store.upsert_hotspot(conn, signal)
            accepted += 1
    return {
        "platform": key,
        "lane": lane_key,
        "source_level": source_level,
        "accepted": accepted,
        "skipped": skipped,
        "strength": _level_strength(source_level),
    }


def _to_float(value) -> float:
    """兼容 "1234.5w" / "980万" / 数字 三种写法。"""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().replace(",", "")
    if not text:
        return 0.0
    multiplier = 1.0
    if text.endswith("w") or text.endswith("W") or text.endswith("万"):
        multiplier = 10_000.0
        text = text[:-1]
    elif text.endswith("亿"):
        multiplier = 100_000_000.0
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return 0.0


def _level_strength(level: str, basis: str = "") -> str:
    # L1 里也有强弱之分：抖音热榜有官方热度值，站内联想词只有位次
    if level == "L1" and basis == "search_rank":
        return "位次信号：能排序不能量化，可作选题依据，但不得写成热度结论"
    return {
        "L1": "可做定量结论",
        "L2": "可做定量结论",
        "L3": "只能作方向提示，不能单独支撑赛道判断",
    }.get(level, "来源不明，不得作为结论依据")


# ─── 归一化 ──────────────────────────────────────────────────────────────────


def _normalize(rows: list[dict]) -> list[dict]:
    """按（平台, 热度口径）分别把热度归一化到 0-100。

    抖音播放量、站内互动分、搜索位次三者不在一个量纲上，直接比大小没有意义，
    所以只在同平台同口径内部比较。rank 靠前但热度值缺失时，用排名兜一个下限。
    """
    buckets: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        buckets.setdefault((row["platform"], meta.get("heat_basis", "hot_value")), []).append(row)
    out: list[dict] = []
    for platform_rows in buckets.values():
        peak = max((r.get("heat_value") or 0) for r in platform_rows)
        for row in platform_rows:
            heat = row.get("heat_value") or 0
            norm = (heat / peak * 100) if peak > 0 else 0.0
            rank = row.get("rank") or 0
            if rank and rank <= 10:
                norm = max(norm, 100.0 - (rank - 1) * 8)
            if len(platform_rows) < 2:
                # 平台只有一条样本时归一化没有比较基准，否则任何单条都会被算成满分。
                # 有排名就看排名，没排名给中性值，避免把“只采到一条”误读成“特别热”。
                norm = (100.0 - (rank - 1) * 8) if rank and rank <= 10 else 50.0
            out.append({**row, "heat_norm": round(min(100.0, max(0.0, norm)), 1)})
    return out


# ─── 趋势 ────────────────────────────────────────────────────────────────────


def trend(lane_name: str = "", days: int = 14, platform: str = "") -> dict:
    """按关键词汇总近期热点：出现天数、最新热度、相对前一次的涨跌。

    第一次采集只有基线，没有涨跌；至少两天数据才能看到 delta。
    """
    key = _check_platform(platform) if platform else None
    with store.connect() as conn:
        rows = store.list_hotspot(
            conn,
            lane=resolve_lane(lane_name).key if lane_name else None,
            platform=key,
            days=days,
            limit=1000,
        )
    rows = _normalize(rows)
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        grouped.setdefault((row["platform"], row["keyword"]), []).append(row)

    items = []
    for (plat, keyword), series in grouped.items():
        series.sort(key=lambda x: x.get("captured_day", ""))
        latest = series[-1]
        prev = series[-2] if len(series) >= 2 else None
        delta = round(latest["heat_norm"] - prev["heat_norm"], 1) if prev else 0.0
        items.append(
            {
                "platform": plat,
                "platform_name": HOTSPOT_PLATFORMS.get(plat, plat),
                "keyword": keyword,
                "latest_day": latest.get("captured_day", ""),
                "heat_norm": latest["heat_norm"],
                "heat_raw": latest.get("heat_raw", ""),
                "rank": latest.get("rank", 0),
                "appearances": len(series),
                "delta": delta,
                "trend": _trend_label(delta, prev is not None),
                "source_levels": sorted({s.get("source_level", "") for s in series}),
                "sustained": len(series) >= 3,
            }
        )
    items.sort(key=lambda x: (x["sustained"], x["heat_norm"]), reverse=True)
    return {
        "window_days": days,
        "lane": lane_name or "",
        "count": len(items),
        "items": items,
        "caveats": _trend_caveats(items),
    }


def _trend_label(delta: float, has_prev: bool) -> str:
    if not has_prev:
        return "基线（首次采集，无对比）"
    if delta >= 8:
        return "上升"
    if delta <= -8:
        return "回落"
    return "持平"


def _trend_caveats(items: list[dict]) -> list[str]:
    caveats = []
    if not items:
        caveats.append("窗口内没有热点数据：先执行 hotspot pull 或 hotspot ingest")
        return caveats
    if any("L3" in i["source_levels"] for i in items):
        caveats.append("含 L3（网页来源）信号，只能作方向提示，不能单独支撑赛道结论")
    baseline_only = [i["keyword"] for i in items if i["appearances"] < 2]
    if baseline_only:
        caveats.append(
            f"{len(baseline_only)} 个词只有单次采集，没有涨跌：至少隔天再采一次才有趋势"
        )
    return caveats


# ─── 错位分析（核心） ─────────────────────────────────────────────────────────


def external_heat(conn, lane_key: str, days: int = 14, top_n: int = 5) -> dict:
    """给 track 用的外部热度旁证。

    默认不进赛道总分（权重 external=0），只作为解释性证据挂在结果里。
    分数取外部平台归一化热度的 top5 均值，不用最大值：单个榜一对赛道判断没意义。
    """
    rows = _normalize(store.list_hotspot(conn, lane=lane_key, days=days, limit=500))
    external = [r for r in rows if r["platform"] != "xhs"]
    if not external:
        return {
            "available": False,
            "score": 0.0,
            "message": "未采集外部平台热点，无法判断外溢",
            "signals": [],
            "source_levels": [],
        }
    ordered = sorted(external, key=lambda x: x["heat_norm"], reverse=True)[:top_n]
    score = round(sum(r["heat_norm"] for r in ordered) / len(ordered), 1)
    return {
        "available": True,
        "score": score,
        "window_days": days,
        "count": len(external),
        "source_levels": sorted({r.get("source_level", "") for r in external}),
        "signals": [
            {
                "keyword": r["keyword"],
                "platform": r["platform"],
                "platform_name": HOTSPOT_PLATFORMS.get(r["platform"], r["platform"]),
                "heat_norm": r["heat_norm"],
                "rank": r.get("rank", 0),
                "raw_title": r.get("raw_title", ""),
            }
            for r in ordered
        ],
        "caveat": "外部热度为同平台内归一化值，只能说明\"在那边热\"，不等于小红书能承接",
    }


def gaps(lane_name: str, days: int = 14, limit: int = 12) -> dict:
    """跨平台错位分析：外部热但站内供给少 = 值得下注的缝隙。

    判定用的是"外部热度 ×(1 - 站内供给充分度)"，不是单看谁更热。
    """
    lane = resolve_lane(lane_name)
    with store.connect() as conn:
        hot_rows = store.list_hotspot(conn, lane=lane.key, days=days, limit=1000)
        lane_notes = store.list_notes(conn, lane=lane.key, limit=800)
    hot_rows = _normalize(hot_rows)

    external = [r for r in hot_rows if r["platform"] != "xhs"]
    if not external:
        return _gaps_without_external(lane, hot_rows, days)

    rows = []
    # 站内一条样本都没采时，supply=0 无法区分“真没供给”与“没去查”，
    # 这种情况一律归为 unverified，不能当成缝隙推荐。
    xhs_sampled = bool(lane_notes)
    for item in external:
        keyword = item["keyword"]
        supply, demand = _xhs_fit(lane_notes, keyword)
        heat = item["heat_norm"]
        lane_key = "opportunity"
        verdict = ""
        if not xhs_sampled:
            lane_key = "unverified"
            verdict = f"外部热度 {heat}，但该赛道尚未采集小红书样本，无法判断供给缺口"
        elif heat >= HOTSPOT_SPILL_HEAT and supply < HOTSPOT_SUPPLY_GAP:
            verdict = f"外部热（{heat}）但站内仅 {supply} 条样本，需求没被接住"
        elif heat >= HOTSPOT_SPILL_HEAT and supply >= HOTSPOT_SUPPLY_GAP:
            lane_key = "saturated"
            verdict = f"外部热（{heat}）且站内已有 {supply} 条，供给充分，要靠差异化切入"
        elif demand > 0 and heat < HOTSPOT_SPILL_HEAT:
            lane_key = "native"
            verdict = f"站内原生需求（中位互动 {int(demand)}），非外部外溢驱动"
        else:
            lane_key = "unverified"
            verdict = "只有外部信号，站内尚未验证，需要先补采小红书样本"
        rows.append(
            {
                "keyword": keyword,
                "platform": item["platform"],
                "platform_name": HOTSPOT_PLATFORMS.get(item["platform"], item["platform"]),
                "heat_norm": heat,
                "rank": item.get("rank", 0),
                "xhs_supply": supply,
                "xhs_median_engage": int(demand),
                "verdict": verdict,
                "category": lane_key,
                "source_level": item.get("source_level", ""),
                "source_url": item.get("source_url", ""),
            }
        )

    buckets = {"opportunity": [], "saturated": [], "native": [], "unverified": []}
    for row in rows:
        buckets[row["category"]].append(row)
    for key in buckets:
        buckets[key].sort(key=lambda x: x["heat_norm"], reverse=True)

    return {
        "lane": lane.key,
        "lane_is_custom": lane.is_custom,
        "window_days": days,
        "available": True,
        "xhs_sampled": xhs_sampled,
        "count": len(rows),
        "opportunities": buckets["opportunity"][:limit],
        "saturated": buckets["saturated"][:limit],
        "native": buckets["native"][:limit],
        "unverified": buckets["unverified"][:limit],
        "verdict": _gaps_verdict(lane, buckets, xhs_sampled),
        "caveats": _gaps_caveats(rows, xhs_sampled),
    }


def _gaps_without_external(lane, hot_rows: list[dict], days: int) -> dict:
    """没有可归属的外部热点时，至少把两侧能用的材料摆出来。

    抖音全国热榜是大众人气榜，细分赛道基本不会上榜，命中 0 条是常态。
    这种情况给不出“外溢缝隙”，但不能因此返回空结果：
    站内搜索需求词依旧是可用的选题材料，热榜池也可以人工扫一眼能否蹭话题。
    """
    station_demand = [
        r
        for r in hot_rows
        if r["platform"] == "xhs"
        and isinstance(r.get("meta"), dict)
        and r["meta"].get("heat_basis") == "search_rank"
    ]
    station_demand.sort(key=lambda x: x.get("rank") or 99)

    with store.connect() as conn:
        pool_rows = store.list_hotspot(conn, platform="douyin", days=days, limit=200)
    pool = [r for r in _normalize(pool_rows) if not r.get("lane")][:20]

    return {
        "lane": lane.key,
        "lane_is_custom": lane.is_custom,
        "window_days": days,
        "available": False,
        "message": (
            "没有可归属到该赛道的外部热点。抖音全国热榜是大众榜，"
            "细分赛道通常不会上榜，这属于正常情况，不代表赛道没有机会"
        ),
        "next": (
            "python scripts/growing_cli.py hotspot hotwords "
            f"--platform douyin --lane {lane.key}"
        ),
        "station_demand": [
            {
                "keyword": r["keyword"],
                "rank": r.get("rank", 0),
                "heat_raw": r.get("heat_raw", ""),
            }
            for r in station_demand
        ],
        "pool": [
            {
                "keyword": r["keyword"],
                "heat_norm": r["heat_norm"],
                "rank": r.get("rank", 0),
            }
            for r in pool
        ],
        "opportunities": [],
        "verdict": {
            "conclusion": "无法判断外溢机会：赛道不在大众热榜上",
            "detail": (
                "外溢判断需要“外部热 + 站内冷”两个条件同时成立。"
                "当前外部侧没有命中，先按站内需求词拆选题，不要据此否定赛道"
            ),
        },
        "caveats": [
            "station_demand 是搜索联想词位次，不是热度值，只能说明搜索顺序偏好",
            "pool 是全网大众热榜，与具体赛道的相关性需人工判断，不能直接当成赛道热点",
        ],
    }


def _xhs_fit(notes: list[dict], keyword: str) -> tuple[int, float]:
    """关键词在小红书站内的供给量与需求水平。

    命中规则：采集关键词精确相同，或标题里出现该热点词。
    宁可少算也不虚高——虚高会把"缝隙"误判成"饱和"。
    """
    hit = [
        n
        for n in notes
        if (n.get("keyword") or "") == keyword or keyword in (n.get("title") or "")
    ]
    if not hit:
        return 0, 0.0
    scores = [
        eng_score(
            n.get("liked", 0),
            n.get("collected", 0),
            n.get("comment", 0),
            n.get("shared", 0),
        )
        for n in hit
    ]
    return len(hit), statistics.median(scores) if scores else 0.0


def _gaps_verdict(lane, buckets: dict, xhs_sampled: bool = True) -> dict:
    opp = buckets["opportunity"]
    if not xhs_sampled:
        return {
            "conclusion": "无法判断：站内还没采集数据",
            "detail": (
                "外部热点已入库，但该赛道在小红书一条样本都没采。"
                "先跑 collect lane 把站内样本采回来，否则 supply=0 分不清是“真的没人做”还是“没去查”"
            ),
            "next": f"python scripts/growing_cli.py collect lane --lane {lane.key}",
        }
    if not opp:
        if buckets["saturated"]:
            return {
                "conclusion": "暂无明显外溢缝隙",
                "detail": (
                    "外部热词在小红书都已有对应供给，这个方向不是'没人做'，"
                    "而是'要做得更好才行'。适合有独特素材或视角的人，不适合纯新手硬切"
                ),
            }
        return {
            "conclusion": "外部热点与该赛道关联弱",
            "detail": "导入的热词和赛道关键词对不上，先确认热点是不是真的属于这个赛道",
        }
    top = opp[0]
    return {
        "conclusion": f"存在 {len(opp)} 个外溢缝隙，优先看「{top['keyword']}」",
        "detail": (
            f"{top['keyword']} 在{top['platform_name']}热度 {top['heat_norm']}，"
            f"小红书站内仅 {top['xhs_supply']} 条相关样本。"
            "这是典型的需求先到、供给没跟上，适合用第 2-4 周去试"
        ),
        "lane": lane.key,
    }


def _gaps_caveats(rows: list[dict], xhs_sampled: bool = True) -> list[str]:
    caveats = [
        "外部热度用的是同平台内归一化后的相对值，不能跨平台比绝对大小",
        "站内供给量是已采集样本的去重计数，不等于平台真实总量；样本越少这个数越不可靠",
    ]
    if not xhs_sampled:
        caveats.insert(
            0,
            "该赛道尚无任何站内样本：所有条目均为“待验证”，不得用于赛道去留的决策",
        )
    if rows and all(r["source_level"] == "L3" for r in rows):
        caveats.append(
            "全部为 L3（网页来源）信号：只能用作选题方向提示，不能写成确定结论"
        )
    platforms = {r["platform"] for r in rows}
    sparse = [p for p in platforms if sum(1 for r in rows if r["platform"] == p) < 2]
    if sparse:
        caveats.append(
            f"{'、'.join(HOTSPOT_PLATFORMS.get(p, p) for p in sparse)}只有单条样本，"
            "热度为中性估计，至少导入 2 条才能做同平台内相对比较"
        )
    unverified = [r for r in rows if r["category"] == "unverified"]
    if unverified and xhs_sampled:
        caveats.append(
            f"{len(unverified)} 个热词站内还没采集到样本，先跑 collect lane 补采再判断"
        )
    return caveats
