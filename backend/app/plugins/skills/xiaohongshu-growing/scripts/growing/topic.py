"""选题池：把赛道数据转成"有数据支撑的题目候选"。

这里只做结构化生成与校验，具体文案由 Agent 在此基础上润色。
每个选题都必须带：所属支柱、搜索词、竞争强度、收藏动机、素材要求。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from statistics import median

from . import store
from .config import resolve_lane
from .metrics import detect_hook_patterns, percentile, title_shingles

# 标题骨架库：pattern 中用 {} 表示待填槽位
TEMPLATES: tuple[dict, ...] = (
    {
        "key": "清单体",
        "pattern": "{target}清单｜{n}个{benefit}",
        "slots": ("target", "n", "benefit"),
        "pillars": ("用品避坑", "好物清单", "区域清单", "收纳方法", "路线规划"),
        "collect": "high",
        "material": "需要 3-8 个真实使用/实拍对象，单个可一句话说清",
    },
    {
        "key": "避坑体",
        "pattern": "{target}别乱买，{n}个坑我替你踩了",
        "slots": ("target", "n"),
        "pillars": ("用品避坑", "装修避坑", "排队避坑", "交通/预算"),
        "collect": "high",
        "material": "需要有真实踩坑经历，能被追问细节",
    },
    {
        "key": "对照体",
        "pattern": "{a}和{b}，我最后选了{choice}",
        "slots": ("a", "b", "choice"),
        "pillars": ("用品避坑", "价格带对比", "住宿选择", "空间改造"),
        "collect": "high",
        "material": "两个真实对比对象 + 决策理由",
    },
    {
        "key": "结果体",
        "pattern": "{time}把{target}做到{result}",
        "slots": ("time", "target", "result"),
        "pillars": ("空间改造", "成长发育", "清洁维护", "目的地攻略"),
        "collect": "mid",
        "material": "需要前后对比图或可量化结果",
    },
    {
        "key": "身份体",
        "pattern": "作为{identity}，我只推荐{n}个{target}",
        "slots": ("identity", "n", "target"),
        "pillars": ("单店测评", "绘本共读", "氛围布置", "隐藏吃法"),
        "collect": "mid",
        "material": "需要一个可信的长期身份标签",
    },
    {
        "key": "场景体",
        "pattern": "{scene}的时候，我会{action}",
        "slots": ("scene", "action"),
        "pillars": ("亲子陪伴", "妈妈自我照顾", "本地时令", "季节限定", "氛围布置"),
        "collect": "mid",
        "material": "真实生活场景，不需要产品",
    },
    {
        "key": "预算体",
        "pattern": "{budget}以内，{target}怎么配",
        "slots": ("budget", "target"),
        "pillars": ("区域清单", "交通/预算", "好物清单", "小户型"),
        "collect": "high",
        "material": "需要真实价格与配置方案",
    },
    {
        "key": "流程体",
        "pattern": "{target}的全流程，照着做就行",
        "slots": ("target",),
        "pillars": ("喂养辅食", "路线规划", "收纳方法", "装修避坑"),
        "collect": "high",
        "material": "需要完整步骤，缺一步内容就废",
    },
)


@dataclass
class Topic:
    """一个候选选题。"""

    id: str
    pillar: str
    template: str
    title: str
    slots: dict[str, str] = field(default_factory=dict)
    search_keyword: str = ""
    competition: dict = field(default_factory=dict)
    collect_intent: str = "mid"
    material: str = ""
    evidence: dict = field(default_factory=dict)
    risk: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pillar": self.pillar,
            "template": self.template,
            "title": self.title,
            "slots": self.slots,
            "search_keyword": self.search_keyword,
            "competition": self.competition,
            "collect_intent": self.collect_intent,
            "material": self.material,
            "evidence": self.evidence,
            "risk": self.risk,
        }


def _pillar_stats(notes: list[dict], lane_key: str) -> dict[str, dict]:
    """每个支柱的供给量、中位点赞、竞争度。"""
    from .benchmark import _classify_pillar

    buckets: dict[str, list[dict]] = {}
    for n in notes:
        pillar = _classify_pillar(lane_key, n.get("title", ""), n.get("tags", []))
        buckets.setdefault(pillar, []).append(n)
    out = {}
    for pillar, items in buckets.items():
        likes = [i.get("liked", 0) for i in items]
        out[pillar] = {
            "count": len(items),
            "median_liked": int(median(likes)) if likes else 0,
            "p90_liked": int(percentile(likes, 0.9)),
            "unique_authors": len({i.get("author_id") or i.get("author_name") for i in items}),
        }
    return out


def _fill_slots(values: dict[str, list[str]], lane_key: str, pillar: str) -> dict[str, str]:
    """用真实出现过的词填槽位，缺省时退化为赛道通用词。"""
    def pick(key: str, fallback: str) -> str:
        items = values.get(key) or []
        return items[0] if items else fallback

    return {
        "target": pick("target", pillar),
        "n": pick("n", "5"),
        "benefit": pick("benefit", "真正好用的"),
        "time": pick("time", "30天"),
        "result": pick("result", "肉眼可见的变化"),
        "a": pick("a", "贵的"),
        "b": pick("b", "便宜的"),
        "choice": pick("choice", "后者"),
        "identity": pick("identity", "普通妈妈"),
        "scene": pick("scene", "下班回家"),
        "action": pick("action", "10 分钟搞定"),
        "budget": pick("budget", "500"),
    }


def _slot_values(notes: list[dict], lane_key: str) -> dict[str, list[str]]:
    """从真实标题与话题里抽可用词，保证生成的题目不脱离赛道语境。"""
    tags = Counter(t for n in notes for t in (n.get("tags") or []) if t)
    titles = [n.get("title", "") for n in notes if n.get("title")]
    numbers = Counter()
    for t in titles:
        for token in ("3", "5", "6", "8", "10"):
            if token in t:
                numbers[token] += 1
    lane = resolve_lane(lane_key) if lane_key else None
    targets = [t for t, _ in tags.most_common(8)]
    if not targets and lane:
        targets = list(lane.keywords[:6])
    return {
        "target": targets,
        "n": [n for n, _ in numbers.most_common(3)] or ["5"],
        "benefit": [t for t in ("真正好用的", "实用", "不踩雷", "省钱")],
        "budget": ["300", "500", "1000"],
        "identity": _identities(lane_key),
    }


def _identities(lane_key: str) -> list[str]:
    return {
        "母婴育儿": ["二胎妈", "新手妈妈", "职场妈妈"],
        "美食探店": ["本地土著", "吃了十年的老客", "探店三年"],
        "旅行出行": ["自驾党", "周末出行党", "穷游党"],
        "家居生活": ["租房党", "小户型住户", "装修过的过来人"],
    }.get(lane_key, ["普通用户"])


def build_pool(
    lane_name: str,
    per_pillar: int = 3,
    exclude_titles: list[str] | None = None,
) -> dict:
    """生成选题池。exclude_titles 用于避开已发过的题目。"""
    lane = resolve_lane(lane_name)
    with store.connect() as conn:
        rows = store.list_notes(conn, lane=lane.key, limit=300)
    notes = [r for r in rows if r.get("keyword") in set(lane.keywords)] or rows
    if not notes:
        return {
            "lane": lane.key,
            "topics": [],
            "message": "该赛道暂无采集数据，先执行 collect lane 再生成选题",
        }

    pillar_stats = _pillar_stats(notes, lane.key)
    slots = _slot_values(notes, lane.key)
    hooks = detect_hook_patterns([n.get("title", "") for n in notes])
    existing = exclude_titles or [n.get("title", "") for n in notes]

    topics: list[Topic] = []
    idx = 0
    for pillar in lane.pillars:
        stat = pillar_stats.get(pillar, {})
        # 该支柱没有数据时也生成，但标注为低置信
        candidates = [t for t in TEMPLATES if pillar in t["pillars"]] or list(TEMPLATES[:2])
        for tpl in candidates[:per_pillar]:
            filled = _fill_slots(slots, lane.key, pillar)
            try:
                title = tpl["pattern"].format(**filled)
            except KeyError:
                continue
            if _too_similar(title, existing):
                continue
            idx += 1
            keyword = _best_keyword(lane.key, pillar, slots)
            topics.append(
                Topic(
                    id=f"T{idx:02d}",
                    pillar=pillar,
                    template=tpl["key"],
                    title=title,
                    slots=filled,
                    search_keyword=keyword,
                    competition={
                        "pillar_notes": stat.get("count", 0),
                        "pillar_median_liked": stat.get("median_liked", 0),
                        "pillar_p90_liked": stat.get("p90_liked", 0),
                        "unique_authors": stat.get("unique_authors", 0),
                    },
                    collect_intent=tpl["collect"],
                    material=tpl["material"],
                    evidence={
                        "lane_median_liked": int(median([n.get("liked", 0) for n in notes])),
                        "sample_size": len(notes),
                        "hook_patterns": [h["pattern"] for h in hooks[:3]],
                    },
                    risk=_risks(title, tpl),
                )
            )
            existing.append(title)
    # 竞争度低 + 收藏动机强的排前面
    topics.sort(key=lambda t: (t.collect_intent != "high", t.competition["pillar_notes"]))
    return {
        "lane": lane.key,
        "audience": lane.audience,
        "pillars": list(lane.pillars),
        "sample_size": len(notes),
        "topics": [t.to_dict() for t in topics],
        "how_to_use": (
            "从中挑 3-5 条作为首周选题：优先选「收藏动机 high + 该支柱已有数据」的组合，"
            "标题可再润色但不要改变钩子类型"
        ),
    }


def _best_keyword(lane_key: str, pillar: str, slots: dict[str, list[str]]) -> str:
    lane = resolve_lane(lane_key)
    for word in lane.keywords:
        if any(word[:2] in p for p in [pillar]):
            return word
    targets = slots.get("target") or []
    return targets[0] if targets else lane.keywords[0]


def _too_similar(title: str, existing: list[str], threshold: float = 0.6) -> bool:
    grams = title_shingles(title)
    if not grams:
        return True
    for other in existing:
        other_grams = title_shingles(other)
        if not other_grams:
            continue
        overlap = len(grams & other_grams) / max(1, min(len(grams), len(other_grams)))
        if overlap >= threshold:
            return True
    return False


def _risks(title: str, tpl: dict) -> list[str]:
    risks = []
    if len(title) > 20:
        risks.append("标题超过 20 字，发布前需压缩或把信息挪到封面")
    if any(word in title for word in ("最", "第一", "100%", "唯一", "绝对")):
        risks.append("含绝对化用语，有被限流风险，建议改为具体数字或条件表述")
    if tpl["collect"] == "high" and "清单" in tpl["pattern"]:
        risks.append("清单类内容必须真的列满，缺项会掉收藏")
    return risks


def pick_for_calendar(lane_name: str, count: int = 15) -> list[dict]:
    """给排期挑选题：支柱轮换 + 收藏动机优先，避免一个支柱连着做。"""
    pool = build_pool(lane_name, per_pillar=3)
    topics = pool.get("topics", [])
    if not topics:
        return []
    by_pillar: dict[str, list[dict]] = {}
    for t in topics:
        by_pillar.setdefault(t["pillar"], []).append(t)
    pillars = list(by_pillar)
    picked: list[dict] = []
    pos = 0
    while len(picked) < count and any(by_pillar.values()):
        pillar = pillars[pos % len(pillars)]
        items = by_pillar.get(pillar) or []
        if items:
            picked.append(items.pop(0))
        pos += 1
    return picked[:count]
